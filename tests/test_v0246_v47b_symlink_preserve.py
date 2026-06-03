# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.46 V47-B (Gap B) — symlink preservation tests.

Hard rule (user decision 2026-06-03): VCO never replaces or follows a
symlink under the install path. The intended content lands at a
``.vco-new`` sibling and a deferral entry is emitted.

Coverage:
  Section 1: helper module ``vco_lib.symlink_handler``
    - ``is_symlink_blocking`` — POSIX symlinks (file, dir, dangling),
      regular files / dirs, non-existent paths.
    - ``compute_vco_new_path`` — file vs directory shape.
    - ``emit_symlink_deferral`` — appends correctly-shaped entry.

  Section 2: install.py write sites guarded by V47-B
    - ``update_merge_notification_block`` redirects to ``.vco-new`` for
      a symlinked CONTEXT_STATE.md.
    - ``_copy_recursive_preserve`` skips a symlinked destination and
      writes to ``.vco-new`` sibling instead.
    - ``_configure_claude_settings`` skips a symlinked
      ``settings.json``.
    - Symlinked ``.claude/`` redirects the whole settings install.
    - Recursion stop: writes under a symlinked directory route to the
      sibling, never through the link.

  Section 3: cross-platform
    - All symlink-creation tests skip cleanly on Windows when
      ``os.symlink`` requires admin/dev-mode.

  Section 4: deferral schema sanity
    - condition_id matches the documented stable string.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import symlink_handler  # noqa: E402
from vco_lib.deferral_report import DeferralReport, DeferralEntry  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_symlink_or_skip(target: Path, link: Path) -> None:
    """Create a symlink or skip the test if the platform refuses.

    On Windows, ``os.symlink`` raises ``OSError`` (or
    ``NotImplementedError`` on older Pythons) when developer-mode is off
    and the process isn't elevated. The whole symlink rule still applies
    on those systems — we just can't exercise it in CI without an admin
    runner. Skip rather than xfail so the test count stays honest.
    """
    try:
        os.symlink(str(target), str(link))
    except (OSError, NotImplementedError) as exc:
        pytest.skip(
            f"cannot create symlink on this platform: {exc} "
            "(Windows requires developer-mode or admin)"
        )


# ---------------------------------------------------------------------------
# Section 1: is_symlink_blocking
# ---------------------------------------------------------------------------

def test_is_symlink_blocking_true_for_file_symlink(tmp_path):
    target = tmp_path / "real_file.txt"
    target.write_text("hello")
    link = tmp_path / "link_to_file"
    _make_symlink_or_skip(target, link)
    assert symlink_handler.is_symlink_blocking(link) is True


def test_is_symlink_blocking_true_for_dir_symlink(tmp_path):
    target = tmp_path / "real_dir"
    target.mkdir()
    link = tmp_path / "link_to_dir"
    _make_symlink_or_skip(target, link)
    assert symlink_handler.is_symlink_blocking(link) is True


def test_is_symlink_blocking_true_for_dangling_symlink(tmp_path):
    """The target was never created — dangling. Still a symlink."""
    link = tmp_path / "dangling_link"
    _make_symlink_or_skip(tmp_path / "nonexistent_target", link)
    assert symlink_handler.is_symlink_blocking(link) is True


def test_is_symlink_blocking_false_for_regular_file(tmp_path):
    p = tmp_path / "regular.txt"
    p.write_text("not a link")
    assert symlink_handler.is_symlink_blocking(p) is False


def test_is_symlink_blocking_false_for_regular_dir(tmp_path):
    p = tmp_path / "regular_dir"
    p.mkdir()
    assert symlink_handler.is_symlink_blocking(p) is False


def test_is_symlink_blocking_false_for_nonexistent_path(tmp_path):
    p = tmp_path / "nothing_here"
    assert symlink_handler.is_symlink_blocking(p) is False


def test_is_symlink_blocking_accepts_string_path(tmp_path):
    """Defensive: the helper uses os.fspath so plain strings work too."""
    target = tmp_path / "real.txt"
    target.write_text("x")
    link = tmp_path / "link"
    _make_symlink_or_skip(target, link)
    assert symlink_handler.is_symlink_blocking(str(link)) is True


# ---------------------------------------------------------------------------
# Section 2: compute_vco_new_path
# ---------------------------------------------------------------------------

def test_compute_vco_new_path_for_file(tmp_path):
    p = tmp_path / "CLAUDE.md"
    result = symlink_handler.compute_vco_new_path(p)
    assert result == tmp_path / "CLAUDE.md.vco-new"


def test_compute_vco_new_path_for_dir(tmp_path):
    p = tmp_path / ".claude" / "agents"
    result = symlink_handler.compute_vco_new_path(p)
    assert result == tmp_path / ".claude" / "agents.vco-new"


def test_compute_vco_new_path_for_dotfile(tmp_path):
    p = tmp_path / ".env"
    result = symlink_handler.compute_vco_new_path(p)
    assert result == tmp_path / ".env.vco-new"


def test_compute_vco_new_path_for_double_ext(tmp_path):
    p = tmp_path / "settings.json"
    result = symlink_handler.compute_vco_new_path(p)
    assert result == tmp_path / "settings.json.vco-new"


def test_compute_vco_new_path_preserves_parent(tmp_path):
    """The sibling must live in the same parent directory as the
    blocked target. This is what makes the .vco-new pattern user-
    discoverable on `ls`.
    """
    deep = tmp_path / "a" / "b" / "c" / "target.md"
    result = symlink_handler.compute_vco_new_path(deep)
    assert result.parent == deep.parent
    assert result.name == "target.md.vco-new"


# ---------------------------------------------------------------------------
# Section 3: emit_symlink_deferral
# ---------------------------------------------------------------------------

def test_emit_symlink_deferral_adds_entry_with_stable_condition_id(tmp_path):
    target = tmp_path / "target_dir"
    target.mkdir()
    link = tmp_path / ".claude"
    _make_symlink_or_skip(target, link)
    vco_new = symlink_handler.compute_vco_new_path(link)

    report = DeferralReport()
    symlink_handler.emit_symlink_deferral(report, link, vco_new, install_root=tmp_path)

    entries = report.entries
    assert len(entries) == 1
    assert entries[0].condition_id == symlink_handler.SYMLINK_PRESERVED_CONDITION_ID
    assert entries[0].condition_id == "symlink_preserved_under_install_path"


def test_emit_symlink_deferral_severity_is_info(tmp_path):
    """The user did nothing wrong — VCO followed its rule. Severity
    should not escalate this above info."""
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    _make_symlink_or_skip(target, link)

    report = DeferralReport()
    symlink_handler.emit_symlink_deferral(report, link, link.with_suffix(".vco-new"))
    assert report.entries[0].severity == "info"


def test_emit_symlink_deferral_includes_paths_and_target(tmp_path):
    """The detected prose should name dest, vco_new, and the symlink
    target (so the user knows what they're reconciling)."""
    target = tmp_path / "shared_workflow"
    target.mkdir()
    link = tmp_path / ".claude" / "agents"
    link.parent.mkdir()
    _make_symlink_or_skip(target, link)
    vco_new = symlink_handler.compute_vco_new_path(link)

    report = DeferralReport()
    symlink_handler.emit_symlink_deferral(report, link, vco_new, install_root=tmp_path)

    detected = report.entries[0].detected
    assert ".claude/agents" in detected or "agents" in detected
    assert "shared_workflow" in detected or str(target) in detected


def test_emit_symlink_deferral_includes_reconciliation_commands(tmp_path):
    target = tmp_path / "real"
    target.mkdir()
    link = tmp_path / "link"
    _make_symlink_or_skip(target, link)
    vco_new = symlink_handler.compute_vco_new_path(link)

    report = DeferralReport()
    symlink_handler.emit_symlink_deferral(report, link, vco_new)

    cmd = report.entries[0].command_to_apply
    # Accept VCO defaults — replace symlink with the .vco-new sibling.
    assert "rm" in cmd
    assert "mv" in cmd
    # Or keep the symlink — delete the .vco-new sibling.
    assert "rm -rf" in cmd or "rm " in cmd


def test_emit_symlink_deferral_multiple_calls_collapse_to_one_entry(tmp_path):
    """DeferralReport.add_entry drops prior entries with the same
    condition_id (last-write-wins). Multiple symlink hits in one run
    should not produce one entry per hit."""
    a = tmp_path / "a"
    a.mkdir()
    b = tmp_path / "b"
    b.mkdir()
    link_a = tmp_path / "linka"
    link_b = tmp_path / "linkb"
    _make_symlink_or_skip(a, link_a)
    _make_symlink_or_skip(b, link_b)

    report = DeferralReport()
    symlink_handler.emit_symlink_deferral(report, link_a, link_a.with_suffix(".vco-new"))
    symlink_handler.emit_symlink_deferral(report, link_b, link_b.with_suffix(".vco-new"))

    # Both calls used the same condition_id; only the last survives.
    assert len(report.entries) == 1


def test_emit_symlink_deferral_handles_unreadable_target(tmp_path):
    """If readlink raises (race, permission), the deferral should still
    emit with a placeholder target rather than crashing."""
    link = tmp_path / "weird_link"

    with mock.patch("os.readlink", side_effect=OSError("denied")):
        report = DeferralReport()
        # Simulate a symlink-shaped path even though readlink fails.
        # We patch is_symlink_blocking's caller path to bypass the
        # actual filesystem check.
        symlink_handler.emit_symlink_deferral(
            report, link, link.with_suffix(".vco-new"), install_root=tmp_path
        )
    assert len(report.entries) == 1
    assert "<unreadable>" in report.entries[0].detected


# ---------------------------------------------------------------------------
# Section 4: install.py integration — _copy_recursive_preserve
# ---------------------------------------------------------------------------

# Lazy-load install.py the same way other V47 tests do.
_INSTALL_PY = REPO_ROOT / "install.py"
_spec = importlib.util.spec_from_file_location("install_py_v47b", _INSTALL_PY)
install_py = importlib.util.module_from_spec(_spec)
sys.modules["install_py_v47b"] = install_py
_spec.loader.exec_module(install_py)


def test_copy_recursive_preserve_writes_to_vco_new_when_dst_is_symlink(tmp_path):
    """The critical bundle-apply site: when the destination of a
    preserve-marked file is a symlink, VCO writes to the .vco-new
    sibling, NOT through the symlink."""
    install_root = tmp_path / "install"
    install_root.mkdir()

    # Source file VCO wants to land.
    src = tmp_path / "src" / "CLAUDE.md"
    src.parent.mkdir()
    src.write_text("VCO_DEFAULT_CONTENT")

    # Pre-existing user content the symlink points at.
    real_user_file = tmp_path / "user_content.md"
    real_user_file.write_text("USER_CUSTOM_CONTENT")

    # Symlink at the destination.
    dst = install_root / "CLAUDE.md"
    _make_symlink_or_skip(real_user_file, dst)

    preserve = ["CLAUDE.md"]
    preserved_present: list[str] = []
    report = DeferralReport()

    files_visited, new_files = install_py._copy_recursive_preserve(
        src, dst, install_root, preserve, preserved_present,
        deferral_report=report,
    )

    assert files_visited == 1
    assert new_files == 1
    # The original symlink must be unchanged.
    assert dst.is_symlink()
    assert os.readlink(str(dst)) == str(real_user_file)
    # The user's content (read through the link) must still be the
    # custom one — VCO did NOT overwrite the target.
    assert real_user_file.read_text() == "USER_CUSTOM_CONTENT"
    # The .vco-new sibling must carry VCO's default content.
    vco_new = install_root / "CLAUDE.md.vco-new"
    assert vco_new.exists()
    assert vco_new.read_text() == "VCO_DEFAULT_CONTENT"
    # A deferral entry was emitted.
    assert len(report.entries) == 1
    assert report.entries[0].condition_id == "symlink_preserved_under_install_path"


def test_copy_recursive_preserve_recursion_stops_at_symlinked_dir(tmp_path):
    """Recursion stop: a symlinked DIRECTORY at the destination must
    not be descended into for writes. VCO mirrors the source tree
    under the .vco-new sibling instead."""
    install_root = tmp_path / "install"
    install_root.mkdir()

    src_dir = tmp_path / "src" / "agents"
    src_dir.mkdir(parents=True)
    (src_dir / "a.md").write_text("agent A")
    (src_dir / "b.md").write_text("agent B")

    real_target_dir = tmp_path / "shared_workflow_agents"
    real_target_dir.mkdir()
    (real_target_dir / "preserve_me.md").write_text("user's agent")

    dst_dir = install_root / "agents"
    _make_symlink_or_skip(real_target_dir, dst_dir)

    report = DeferralReport()

    install_py._copy_recursive_preserve(
        src_dir, dst_dir, install_root, preserve=[], preserved_present=[],
        deferral_report=report,
    )

    # The symlink must be unchanged.
    assert dst_dir.is_symlink()
    # The pointed-at directory must NOT have new files.
    assert sorted(p.name for p in real_target_dir.iterdir()) == ["preserve_me.md"]
    # The sibling .vco-new dir carries VCO's content.
    vco_new_dir = install_root / "agents.vco-new"
    assert vco_new_dir.is_dir()
    assert sorted(p.name for p in vco_new_dir.iterdir()) == ["a.md", "b.md"]
    assert (vco_new_dir / "a.md").read_text() == "agent A"
    # Deferral emitted.
    assert any(
        e.condition_id == "symlink_preserved_under_install_path"
        for e in report.entries
    )


# ---------------------------------------------------------------------------
# Section 5: install.py integration — update_merge_notification_block
# ---------------------------------------------------------------------------

def test_update_merge_notification_redirects_when_path_is_symlink(tmp_path):
    real = tmp_path / "real_context.md"
    real.write_text("USER_CONTEXT\n")
    link = tmp_path / "CONTEXT_STATE.md"
    _make_symlink_or_skip(real, link)

    report = DeferralReport()
    result = install_py.update_merge_notification_block(
        link, ["some/file"], deferral_report=report,
    )

    assert result is True
    # Original symlink unchanged; target unchanged.
    assert link.is_symlink()
    assert real.read_text() == "USER_CONTEXT\n"
    # .vco-new sibling carries VCO's block.
    vco_new = tmp_path / "CONTEXT_STATE.md.vco-new"
    assert vco_new.exists()
    assert "some/file" in vco_new.read_text()
    # Deferral emitted.
    assert len(report.entries) == 1


def test_update_merge_notification_uses_lexists_not_exists(tmp_path):
    """A dangling symlink must NOT be treated as "no file here" — that
    would lead VCO to write a fresh file at the symlink path (which
    `Path.write_text` follows to a non-existent target and may either
    fail or write at the target's parent, depending on OS). The
    lexists-equivalent guard prevents the silent-create pattern."""
    real = tmp_path / "nonexistent_target"
    link = tmp_path / "CONTEXT_STATE.md"
    _make_symlink_or_skip(real, link)
    # Sanity: link is dangling.
    assert not link.exists()  # exists() follows symlinks → False
    assert os.path.lexists(str(link))  # lexists doesn't follow → True

    report = DeferralReport()
    install_py.update_merge_notification_block(
        link, ["x"], deferral_report=report,
    )

    # The dangling symlink itself must be unchanged.
    assert link.is_symlink()
    assert os.readlink(str(link)) == str(real)
    # The .vco-new sibling carries the content.
    vco_new = tmp_path / "CONTEXT_STATE.md.vco-new"
    assert vco_new.exists()


# ---------------------------------------------------------------------------
# Section 6: install.py integration — _configure_claude_settings
# ---------------------------------------------------------------------------

def test_configure_claude_settings_redirects_when_settings_is_symlink(tmp_path):
    """A symlinked settings.json must not be overwritten; VCO writes
    its content to settings.json.vco-new instead."""
    fake_project = tmp_path / "project"
    claude_dir = fake_project / ".claude"
    claude_dir.mkdir(parents=True)

    # Pre-existing user file.
    real_settings = tmp_path / "shared_settings.json"
    real_settings.write_text(json.dumps({"user": "custom"}))

    settings_link = claude_dir / "settings.json"
    _make_symlink_or_skip(real_settings, settings_link)

    embed_config = {
        "text_model": "qwen3-embedding:0.6b",
        "active_embedding": "qwen3",
        "code_backend": "ollama",
    }

    report = DeferralReport()
    with mock.patch.object(install_py, "PROJECT_ROOT", fake_project):
        install_py._configure_claude_settings(
            embed_config, deferral_report=report,
        )

    # Original symlink unchanged.
    assert settings_link.is_symlink()
    # Target file content unchanged.
    assert json.loads(real_settings.read_text()) == {"user": "custom"}
    # .vco-new sibling has VCO's config.
    vco_new = claude_dir / "settings.json.vco-new"
    assert vco_new.exists()
    vco_content = json.loads(vco_new.read_text())
    assert "env" in vco_content
    # Deferral emitted.
    assert any(
        e.condition_id == "symlink_preserved_under_install_path"
        for e in report.entries
    )


def test_configure_claude_settings_redirects_when_claude_dir_is_symlink(tmp_path):
    """A symlinked .claude/ directory must redirect the whole write."""
    fake_project = tmp_path / "project"
    fake_project.mkdir()

    real_claude = tmp_path / "shared_claude_dir"
    real_claude.mkdir()
    (real_claude / "existing_user_file.md").write_text("preserve me")

    claude_link = fake_project / ".claude"
    _make_symlink_or_skip(real_claude, claude_link)

    embed_config = {
        "text_model": "qwen3-embedding:0.6b",
        "active_embedding": "qwen3",
        "code_backend": "ollama",
    }

    report = DeferralReport()
    with mock.patch.object(install_py, "PROJECT_ROOT", fake_project):
        install_py._configure_claude_settings(
            embed_config, deferral_report=report,
        )

    # Original .claude symlink unchanged.
    assert claude_link.is_symlink()
    # User's pre-existing file in the shared dir is unchanged.
    assert (real_claude / "existing_user_file.md").read_text() == "preserve me"
    # No new files appeared in the shared dir.
    assert sorted(p.name for p in real_claude.iterdir()) == [
        "existing_user_file.md"
    ]
    # The .vco-new sibling has VCO's settings.json.
    vco_new_dir = fake_project / ".claude.vco-new"
    assert vco_new_dir.exists()
    assert (vco_new_dir / "settings.json").exists()
    # Deferral emitted.
    assert any(
        e.condition_id == "symlink_preserved_under_install_path"
        for e in report.entries
    )


def test_configure_claude_settings_adopt_project_mode_is_agnostic(tmp_path):
    """The symlink rule must apply regardless of adopt_project_mode —
    no mode-specific override. Per user 2026-06-03 hard rule."""
    fake_project = tmp_path / "project"
    claude_dir = fake_project / ".claude"
    claude_dir.mkdir(parents=True)

    real_settings = tmp_path / "shared.json"
    real_settings.write_text("{}")
    settings_link = claude_dir / "settings.json"
    _make_symlink_or_skip(real_settings, settings_link)

    embed_config = {
        "text_model": "qwen3-embedding:0.6b",
        "active_embedding": "qwen3",
        "code_backend": "ollama",
    }

    # Try every documented mode value — symlink must be left alone.
    for mode in ("adopt", "no-adopt", "replace-all", "dry-run", None):
        # Reset between iterations — recreate any sibling VCO wrote.
        vco_new = claude_dir / "settings.json.vco-new"
        if vco_new.exists():
            vco_new.unlink()

        report = DeferralReport()
        with mock.patch.object(install_py, "PROJECT_ROOT", fake_project):
            install_py._configure_claude_settings(
                embed_config,
                adopt_project_mode=mode,
                deferral_report=report,
            )

        assert settings_link.is_symlink(), (
            f"mode={mode!r}: symlink was replaced (VCO must NEVER do that)"
        )


# ---------------------------------------------------------------------------
# Section 7: lexists semantics
# ---------------------------------------------------------------------------

def test_lexists_vs_exists_dangling_symlink(tmp_path):
    """Document the load-bearing semantic: lexists is the right gate
    because exists follows symlinks (a dangling symlink returns False
    for exists, which would mislead write-decisions)."""
    link = tmp_path / "dangling"
    _make_symlink_or_skip(tmp_path / "no_target", link)

    assert link.exists() is False  # follows to non-existent target
    assert os.path.lexists(str(link)) is True
    # is_symlink_blocking is implemented with islink (not lexists), so
    # it correctly returns True for dangling links:
    assert symlink_handler.is_symlink_blocking(link) is True


def test_lexists_vs_exists_regular_file(tmp_path):
    """Sanity: both lexists and exists agree on regular files."""
    p = tmp_path / "regular.txt"
    p.write_text("hi")
    assert p.exists() is True
    assert os.path.lexists(str(p)) is True
    assert symlink_handler.is_symlink_blocking(p) is False


# ---------------------------------------------------------------------------
# Section 8: deferral entry format sanity (pinned strings)
# ---------------------------------------------------------------------------

def test_condition_id_constant_is_documented_string():
    """Stable string — must not be renamed without a migration entry."""
    assert symlink_handler.SYMLINK_PRESERVED_CONDITION_ID == \
        "symlink_preserved_under_install_path"


def test_vco_new_suffix_constant_is_documented_string():
    """Tests, launcher reader, and future --apply-deferred all rely
    on this suffix being exactly ``.vco-new``."""
    assert symlink_handler.VCO_NEW_SUFFIX == ".vco-new"


def test_deferral_entry_renders_in_report_write(tmp_path):
    """End-to-end: emit_symlink_deferral → DeferralReport.write must
    produce a readable UPDATE_DEFERRED.md with the symlink section."""
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / ".claude" / "agents"
    link.parent.mkdir()
    _make_symlink_or_skip(target, link)
    vco_new = symlink_handler.compute_vco_new_path(link)

    report = DeferralReport()
    symlink_handler.emit_symlink_deferral(
        report, link, vco_new, install_root=tmp_path,
    )

    # Write the report into a fake project root.
    project_root = tmp_path / "fake_proj"
    project_root.mkdir()
    written = report.write(project_root)
    assert written is True

    deferred_md = project_root / ".claude" / "context" / "UPDATE_DEFERRED.md"
    assert deferred_md.exists()
    body = deferred_md.read_text(encoding="utf-8")
    assert "symlink_preserved_under_install_path" in body
    assert "Symlink preserved under install path" in body
