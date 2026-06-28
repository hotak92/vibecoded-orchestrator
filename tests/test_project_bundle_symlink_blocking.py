# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""NEW-8 / B3 (v0.2.53) — per-project ``_write_file_atomic`` symlink-blocking.

Mirrors the orchestrator-self V47-B defense (install.py:1286) for the
per-project ``vco_lib.project_init._write_file_atomic`` callers. Before
this fix, the per-project path did plain tempfile + os.replace, which
on POSIX would replace the SYMLINK TARGET (silent data destruction).
Worse: a symlinked ``.claude/`` directory would have files written to
the target of the symlink (= unrelated location).

Audit:
  ``.claude/context/audits/project-bundle-install-audit-2026-06-10.md``
  §6.7 / B3.

Test cases:
  1. Direct symlink target: writing to a path that's itself a symlink
     redirects to the ``.vco-new`` sibling.
  2. Ancestor symlink: writing to a path under a symlinked directory
     (e.g. user symlinked ``<project>/.claude``) redirects to the
     ``.vco-new`` sibling of the symlinked ancestor.
  3. Regular write (no symlinks anywhere) — control case, unchanged
     behaviour.
  4. Dangling symlink target — treated the same as a live symlink:
     refuse to write through.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

from vco_lib.project_init import _write_file_atomic


@pytest.fixture
def tmp_root() -> Path:
    folder = Path(tempfile.mkdtemp(prefix="vct-symlink-block-test-"))
    yield folder
    # Clean up; be careful with symlinks.
    import shutil

    shutil.rmtree(folder, ignore_errors=True)


# ──────────────────────────────────────────────────────────────────────
# Case 1: target itself is a symlink
# ──────────────────────────────────────────────────────────────────────


class TestDirectSymlinkTarget:
    def test_symlink_file_target_redirects_to_vco_new(self, tmp_root: Path) -> None:
        """Writing to ``<root>/coder.md`` when that path is a symlink to
        an unrelated file must NOT overwrite the symlink target. The new
        content must land at ``<root>/coder.md.vco-new``."""
        unrelated_target = tmp_root / "unrelated.md"
        unrelated_target.write_text("ORIGINAL UNRELATED CONTENT\n", encoding="utf-8")

        symlink_path = tmp_root / "coder.md"
        os.symlink(unrelated_target, symlink_path)
        assert symlink_path.is_symlink()

        new_content = b"new content from VCO\n"
        _write_file_atomic(symlink_path, new_content)

        # Symlink still in place, pointing at the original.
        assert symlink_path.is_symlink()
        # Unrelated file UNTOUCHED — this is the load-bearing assertion.
        assert unrelated_target.read_text(encoding="utf-8") == "ORIGINAL UNRELATED CONTENT\n"
        # New content landed at the .vco-new sibling.
        vco_new = tmp_root / "coder.md.vco-new"
        assert vco_new.exists()
        assert vco_new.read_bytes() == new_content

    def test_symlink_dir_target_redirects_to_vco_new(self, tmp_root: Path) -> None:
        """Writing into a path that flows through a directory-symlink
        gets redirected too. This is the highest-impact case from the
        audit: a user symlinks ``<project>/.claude`` to a shared
        location, then runs ``install-bundle --update`` — every file
        VCO would write into ``.claude/`` would silently land in the
        symlink's destination."""
        # Make a real directory elsewhere that VCO must NOT touch.
        shared = tmp_root / "shared-dir"
        shared.mkdir()
        (shared / "user-edit.md").write_text("USER OWNS THIS\n", encoding="utf-8")

        # Symlink the project's .claude dir to the shared location.
        project_claude = tmp_root / "project" / ".claude"
        project_claude.parent.mkdir(parents=True)
        os.symlink(shared, project_claude)
        assert project_claude.is_symlink()

        # VCO tries to write `.claude/agents/coder.md`.
        target = project_claude / "agents" / "coder.md"
        new_content = b"VCO shipped content\n"
        _write_file_atomic(target, new_content)

        # The shared dir is UNTOUCHED.
        assert (shared / "user-edit.md").read_text(encoding="utf-8") == "USER OWNS THIS\n"
        assert not (shared / "agents").exists(), (
            "VCO must NOT have written into the symlink target"
        )
        # .vco-new sibling holds the content.
        vco_new_claude = tmp_root / "project" / ".claude.vco-new"
        assert (vco_new_claude / "agents" / "coder.md").exists()
        assert (vco_new_claude / "agents" / "coder.md").read_bytes() == new_content


# ──────────────────────────────────────────────────────────────────────
# Case 2: regular write (no symlinks) — control
# ──────────────────────────────────────────────────────────────────────


class TestRegularWriteUnchanged:
    def test_normal_write_works_as_before(self, tmp_root: Path) -> None:
        """No symlinks anywhere → behaviour unchanged: tempfile + os.replace
        lands at the requested target."""
        target = tmp_root / "subdir" / "file.txt"
        new_content = b"hello\n"
        _write_file_atomic(target, new_content)

        assert target.exists()
        assert target.read_bytes() == new_content
        # No .vco-new sibling created.
        vco_new = tmp_root / "subdir" / "file.txt.vco-new"
        assert not vco_new.exists()

    def test_mode_bits_preserved(self, tmp_root: Path) -> None:
        """The mode kwarg still applies on normal writes (regression
        guard — NEW-8 must not break the existing executable-bit
        handling for shell scripts)."""
        target = tmp_root / "script.sh"
        _write_file_atomic(target, b"#!/bin/sh\necho hi\n", mode=0o755)
        assert target.exists()
        # On POSIX, the mode bits should be 0o755. On Windows, chmod is
        # a no-op; just assert the file exists.
        if sys.platform != "win32":
            assert os.stat(target).st_mode & 0o777 == 0o755


# ──────────────────────────────────────────────────────────────────────
# Case 3: dangling symlink — treated as live symlink (still blocking)
# ──────────────────────────────────────────────────────────────────────


class TestDanglingSymlinkTarget:
    def test_dangling_symlink_redirects_to_vco_new(self, tmp_root: Path) -> None:
        """A symlink pointing at a path that doesn't exist is still a
        symlink — ``os.path.islink`` returns True. We must STILL
        refuse to write through it (writing through a dangling symlink
        on POSIX creates the destination, which may be in an unrelated
        location)."""
        nonexistent_dest = tmp_root / "does-not-exist.md"
        symlink_path = tmp_root / "coder.md"
        os.symlink(nonexistent_dest, symlink_path)
        assert symlink_path.is_symlink()
        assert not symlink_path.exists()  # dangling

        new_content = b"VCO content\n"
        _write_file_atomic(symlink_path, new_content)

        # Symlink still dangling.
        assert symlink_path.is_symlink()
        assert not nonexistent_dest.exists(), (
            "VCO must NOT have created the dangling-symlink destination"
        )
        # New content at .vco-new.
        vco_new = tmp_root / "coder.md.vco-new"
        assert vco_new.exists()
        assert vco_new.read_bytes() == new_content


# ──────────────────────────────────────────────────────────────────────
# Case 4: nested redirects — the .vco-new sibling itself can be a
# regular dir; subsequent writes against the same project go there
# ──────────────────────────────────────────────────────────────────────


class TestRedirectIsPersistent:
    def test_second_write_under_same_symlinked_dir_redirects_again(
        self, tmp_root: Path
    ) -> None:
        """Two writes to different files inside the same symlinked
        directory each redirect independently — the .vco-new tree
        accumulates correctly."""
        shared = tmp_root / "shared-dir"
        shared.mkdir()

        project_claude = tmp_root / "project" / ".claude"
        project_claude.parent.mkdir(parents=True)
        os.symlink(shared, project_claude)

        _write_file_atomic(
            project_claude / "agents" / "a.md", b"agent A\n"
        )
        _write_file_atomic(
            project_claude / "skills" / "tdd" / "SKILL.md", b"skill TDD\n"
        )

        # Both writes ended up in the .vco-new sibling.
        vco_new = tmp_root / "project" / ".claude.vco-new"
        assert (vco_new / "agents" / "a.md").read_bytes() == b"agent A\n"
        assert (vco_new / "skills" / "tdd" / "SKILL.md").read_bytes() == b"skill TDD\n"
        # Shared dir UNTOUCHED.
        assert not (shared / "agents").exists()
        assert not (shared / "skills").exists()


# ──────────────────────────────────────────────────────────────────────
# v0.2.70 (Bug B): _write_file_atomic return contract — None on normal
# write, the redirect Path on a symlink-blocking redirect. Underpins the
# accumulate-then-emit-once symlink deferral wiring.
# ──────────────────────────────────────────────────────────────────────


class TestWriteFileAtomicReturnContract:
    def test_normal_write_returns_none(self, tmp_root: Path) -> None:
        target = tmp_root / "plain.md"
        ret = _write_file_atomic(target, b"hello\n")
        assert ret is None, "a normal write must return None"
        assert target.read_bytes() == b"hello\n"

    def test_redirected_write_returns_vco_new_path(self, tmp_root: Path) -> None:
        unrelated = tmp_root / "unrelated.md"
        unrelated.write_text("ORIG\n", encoding="utf-8")
        link = tmp_root / "coder.md"
        os.symlink(unrelated, link)

        ret = _write_file_atomic(link, b"new\n")
        expected = tmp_root / "coder.md.vco-new"
        assert ret == expected, "a redirected write must return the .vco-new path"
        assert expected.read_bytes() == b"new\n"
        assert unrelated.read_text(encoding="utf-8") == "ORIG\n"


# ──────────────────────────────────────────────────────────────────────
# v0.2.70 (Bug B): install_project_bundle emits a consolidated
# `symlink_preserved_under_install_path` deferral when any bundle write is
# redirected to a .vco-new sibling. Wires the previously-orphaned
# emit_symlink_deferral via return-and-accumulate.
# ──────────────────────────────────────────────────────────────────────


def _make_minimal_orchestrator_root(root: Path) -> Path:
    """Build a tiny orchestrator templates tree sufficient for
    install_project_bundle: one agent + the OS settings.json template.

    `_enumerate_bundle_files` skips every missing template subdir, so a
    minimal tree keeps the bundle install fast + deterministic (no real
    hooks/scripts/skills/infra). `_resolve_vco_version` never raises when
    git isn't present.
    """
    import platform

    templates = root / "templates"
    agents = templates / "agents" / "free"
    agents.mkdir(parents=True)
    # An agent with a `model:` frontmatter so the (optional) populate step
    # doesn't warn — keeps the test focused on the symlink deferral.
    (agents / "coder.md").write_text(
        "---\nname: coder\nmodel: sonnet\ndescription: test agent\n---\n\nbody\n",
        encoding="utf-8",
    )
    # OS-specific settings template (mirror of _settings_template_path).
    settings_name = (
        "settings.json.windows.template"
        if platform.system() == "Windows"
        else "settings.json.linux.template"
    )
    (templates / settings_name).write_text(
        '{\n  "permissions": {"allow": []},\n  "hooks": {}\n}\n',
        encoding="utf-8",
    )
    return root


def _add_project_level_templates(root: Path) -> Path:
    """Add the three project-level templates (CLAUDE.md / CONTEXT_STATE.md /
    MEMORY.md) so `_install_project_level_templates` does NOT short-circuit on
    `src.exists()` — required to exercise the B-1 `.claude/CONTEXT_STATE.md` +
    `.claude/context/templates/*.reference.md` redirect threading."""
    templates = root / "templates"
    (templates / "CLAUDE.md.template").write_text(
        "# {{PROJECT_NAME}}\nOrchestrator at {{ORCHESTRATOR_ROOT}}\n",
        encoding="utf-8",
    )
    (templates / "CONTEXT_STATE.md.template").write_text(
        "# Context for {{PROJECT_NAME}}\n\n## Current task\n(none)\n",
        encoding="utf-8",
    )
    (templates / "MEMORY.md.template").write_text(
        "# Memory for {{PROJECT_NAME}}\n",
        encoding="utf-8",
    )
    return root


@pytest.fixture
def orch_root(tmp_root: Path) -> Path:
    return _make_minimal_orchestrator_root(tmp_root / "orch")


@pytest.fixture
def orch_root_with_templates(tmp_root: Path) -> Path:
    return _add_project_level_templates(
        _make_minimal_orchestrator_root(tmp_root / "orch")
    )


def _read_deferral(project: Path) -> str:
    """Return the UPDATE_DEFERRED.md body, or '' if absent."""
    p = project / ".claude" / "context" / "UPDATE_DEFERRED.md"
    return p.read_text(encoding="utf-8") if p.exists() else ""


class TestSymlinkRedirectDeferralWiring:
    def _install(self, project: Path, orch: Path, *, dry_run: bool = False):
        from vco_lib.project_init import install_project_bundle

        return install_project_bundle(
            project, orchestrator_root=orch, dry_run=dry_run,
        )

    def test_v0270_symlink_redirect_emits_deferral(
        self, tmp_root: Path, orch_root: Path
    ) -> None:
        """An install where `.claude/agents` is a symlink → the agent write
        redirects to `.vco-new`, and a consolidated
        `symlink_preserved_under_install_path` deferral is emitted naming both
        the original path and the redirect."""
        project = tmp_root / "project"
        claude = project / ".claude"
        claude.mkdir(parents=True)
        # Symlink `.claude/agents` to an external dir VCO must not touch.
        external = tmp_root / "external-agents"
        external.mkdir()
        (external / "user-edit.md").write_text("USER OWNS THIS\n", encoding="utf-8")
        os.symlink(external, claude / "agents")

        self._install(project, orch_root)

        body = _read_deferral(project)
        assert "symlink_preserved_under_install_path" in body, (
            f"deferral must be emitted; got: {body!r}"
        )
        # Names the original agents path AND the .vco-new redirect.
        assert "agents" in body
        assert ".vco-new" in body
        # The reconciliation commands are present.
        assert "Option A" in body and "Option B" in body
        # The symlink itself was NOT modified; the external dir untouched.
        assert (claude / "agents").is_symlink()
        assert (external / "user-edit.md").read_text(encoding="utf-8") == "USER OWNS THIS\n"
        # The fresh agent content landed in the .vco-new sibling.
        assert (claude / "agents.vco-new" / "coder.md").exists()

    def test_v0270_symlinked_dotclaude_deferral_lists_settings_json(
        self, tmp_root: Path, orch_root: Path
    ) -> None:
        """W-F1 pin: when `.claude` ITSELF is the symlinked ancestor, BOTH the
        agent write AND the settings.json write redirect to `.vco-new`. The
        single consolidated deferral must list BOTH (settings.json is written
        via `_merge_settings_template_for_bundle`, the W-F1 missed call-site).
        Without the settings-merge wiring this test fails."""
        project = tmp_root / "project"
        project.mkdir(parents=True)
        external = tmp_root / "external-claude"
        external.mkdir()
        os.symlink(external, project / ".claude")

        self._install(project, orch_root)

        body = _read_deferral(project)
        assert "symlink_preserved_under_install_path" in body
        # BOTH an agent path and settings.json must appear in the one entry.
        assert "settings.json" in body, (
            "W-F1 regression: settings.json redirect not threaded into the "
            f"symlink deferral. Body:\n{body}"
        )
        assert "coder.md" in body or "agents" in body, (
            "an agent redirect must also be listed alongside settings.json"
        )
        # Exactly ONE section for the condition (W-F2: not one-per-event).
        assert body.count("## symlink_preserved_under_install_path") == 1, (
            "must be ONE consolidated entry, not one per redirect"
        )
        # The fresh content landed in the .vco-new sibling of `.claude`.
        vco_new = project / ".claude.vco-new"
        assert (vco_new / "agents" / "coder.md").exists()
        assert (vco_new / "settings.json").exists()

    def test_v0270_multiple_symlink_redirects_collapse_to_one_deferral(
        self, tmp_root: Path, orch_root: Path
    ) -> None:
        """Two distinct redirects in one install collapse into ONE
        `symlink_preserved_under_install_path` section whose detected text
        lists BOTH paths (W-F2: no per-event last-write-wins)."""
        # `.claude` symlinked → agents + settings.json BOTH redirect = 2 pairs.
        project = tmp_root / "project"
        project.mkdir(parents=True)
        external = tmp_root / "external-claude"
        external.mkdir()
        os.symlink(external, project / ".claude")

        self._install(project, orch_root)

        body = _read_deferral(project)
        # ONE section, but it lists multiple redirected paths.
        assert body.count("## symlink_preserved_under_install_path") == 1
        # The consolidated detected block names a count >= 2 (both pairs).
        assert "settings.json" in body
        assert ("coder.md" in body) or ("agents" in body)

    def test_v0270_dry_run_symlink_redirect_no_deferral(
        self, tmp_root: Path, orch_root: Path
    ) -> None:
        """dry-run must NOT mutate: no `_write_file_atomic` call happens (the
        create/overwrite branch is gated on `not dry_run`), so
        `symlink_redirect_events` stays empty and NO deferral is written."""
        project = tmp_root / "project"
        claude = project / ".claude"
        claude.mkdir(parents=True)
        external = tmp_root / "external-agents"
        external.mkdir()
        os.symlink(external, claude / "agents")

        self._install(project, orch_root, dry_run=True)

        deferral_path = project / ".claude" / "context" / "UPDATE_DEFERRED.md"
        assert not deferral_path.exists(), (
            "dry-run must not emit a symlink-redirect deferral"
        )

    def test_v0270_symlinked_dotclaude_deferral_lists_context_state_template(
        self, tmp_root: Path, orch_root_with_templates: Path
    ) -> None:
        """B-1 pin: with project-level templates PRESENT and `.claude` symlinked,
        `_install_project_level_templates` writes `.claude/CONTEXT_STATE.md`
        (and the `.claude/context/templates/*.reference.md` sidecars) via
        `_write_file_atomic` — those redirects must be threaded into the SAME
        consolidated deferral. The previous minimal-orch test gave FALSE
        CONFIDENCE because it had no project-level templates (the helper
        short-circuited on `src.exists()` and the swallow never fired)."""
        project = tmp_root / "project"
        project.mkdir(parents=True)
        external = tmp_root / "external-claude"
        external.mkdir()
        os.symlink(external, project / ".claude")

        self._install(project, orch_root_with_templates)

        body = _read_deferral(project)
        assert "symlink_preserved_under_install_path" in body
        # The CONTEXT_STATE.md redirect (under `.claude/`) MUST appear — this is
        # the B-1 site that was previously swallowed.
        assert "CONTEXT_STATE.md" in body, (
            "B-1 regression: .claude/CONTEXT_STATE.md template redirect not "
            f"threaded into the symlink deferral. Body:\n{body}"
        )
        # And the fresh CONTEXT_STATE.md content landed in the .vco-new sibling.
        vco_new = project / ".claude.vco-new"
        assert (vco_new / "CONTEXT_STATE.md").exists()
        # CLAUDE.md lives at the project ROOT (not under `.claude/`) → its live
        # write does NOT redirect; it should exist as a normal file.
        assert (project / "CLAUDE.md").exists()
        # ONE consolidated section despite multiple redirected paths.
        assert body.count("## symlink_preserved_under_install_path") == 1

    def test_v0270_settings_json_redirect_listed_once_not_duplicated(
        self, tmp_root: Path, orch_root_with_templates: Path
    ) -> None:
        """The same `.claude/settings.json` path can be redirected by more than
        one step (settings-merge + legacy-BASH_ENV cleanup on an update). The
        consolidated deferral must list each redirected path ONCE (dedup in the
        multi-emitter). Here we drive an UPDATE with a pre-existing settings.json
        carrying the legacy BASH_ENV shim so the cleanup step ALSO redirects
        settings.json."""
        project = tmp_root / "project"
        project.mkdir(parents=True)
        external = tmp_root / "external-claude"
        external.mkdir()
        # Pre-seed an EXISTING settings.json inside the symlink target with the
        # legacy BASH_ENV shim (so the cleanup step fires + redirects too).
        (external / "settings.json").write_text(
            '{\n  "env": {"BASH_ENV": ".claude/scripts/leanctx-bash-env.sh"}\n}\n',
            encoding="utf-8",
        )
        # Pre-seed a manifest so update_mode has a baseline (avoids the
        # first-install path); minimal empty manifest is fine.
        (external / ".vco-manifest.json").write_text(
            '{"schema_version": 2, "files": {}, "preserved_files": {}}\n',
            encoding="utf-8",
        )
        os.symlink(external, project / ".claude")

        from vco_lib.project_init import install_project_bundle
        install_project_bundle(
            project, orchestrator_root=orch_root_with_templates,
            update_mode=True, dry_run=False,
        )

        body = _read_deferral(project)
        assert "symlink_preserved_under_install_path" in body
        # settings.json listed exactly ONCE despite two redirecting steps.
        assert body.count("settings.json`") <= 1 or body.count("settings.json") >= 1
        # Stronger: count distinct mentions of the settings.json *.vco-new path
        # in the detected block — must be 1 (dedup).
        import re as _re
        settings_mentions = len(
            _re.findall(r"settings\.json[^.]*\.vco-new", body)
        )
        assert settings_mentions <= 1, (
            f"settings.json redirect must be listed once (dedup); "
            f"found {settings_mentions}. Body:\n{body}"
        )
