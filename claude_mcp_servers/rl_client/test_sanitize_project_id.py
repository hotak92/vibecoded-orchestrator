# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for the v0.2.49 ``sanitize_project_id`` helper.

The container (vct-rl-reranker v0.2.10+) reads the
``X-VCT-Project-ID`` header VERBATIM and uses the value as a
filesystem path component (``/data/state/projects/<id>/``) and as a
JSONL filename suffix (``rl_events_<id>.jsonl``). Container code does
NOT sanitise its own input; Stream C explicitly flagged this as
launcher-side responsibility.

These tests pin the launcher-side sanitiser's character set: accept
UUIDs + alphanumeric slugs (the launcher's own ``projects.slug`` /
``projects.id`` shapes), reject everything else — path separators,
control chars, unicode, spaces, dots, empty / overlong strings.

Failure mode for any malformed input: return ``None`` (NOT raise).
The RLClient then sends NO ``X-VCT-Project-ID`` header at all, and
the container falls back to the base model. This is the intended
fail-safe behaviour — a malformed project_id results in degraded but
SAFE operation (base model), never in container-side path traversal.
"""
from __future__ import annotations

import pytest

from claude_mcp_servers.rl_client.client import sanitize_project_id


class TestSanitizeProjectIdHappyPath:
    """Inputs that should be accepted verbatim."""

    def test_uuid_v4_canonical(self) -> None:
        """The shape ``projects.id`` carries by default in launcher.db."""
        got = sanitize_project_id("02fbc934-ada5-433c-b606-d1f56194035a")
        assert got == "02fbc934-ada5-433c-b606-d1f56194035a"

    def test_uuid_v4_uppercase(self) -> None:
        """Case-insensitive hex letters."""
        got = sanitize_project_id("02FBC934-ADA5-433C-B606-D1F56194035A")
        assert got == "02FBC934-ADA5-433C-B606-D1F56194035A"

    def test_simple_slug(self) -> None:
        """Slug shape ``projects.slug`` carries (orchestrator-root, sd15, …)."""
        got = sanitize_project_id("orchestrator-root")
        assert got == "orchestrator-root"

    def test_alphanumeric_only(self) -> None:
        got = sanitize_project_id("sd15")
        assert got == "sd15"

    def test_slug_with_underscores(self) -> None:
        """Underscores are part of the accepted set (some legacy slugs)."""
        got = sanitize_project_id("project_one")
        assert got == "project_one"

    def test_single_char(self) -> None:
        """1-char input is the minimum length; should be accepted."""
        got = sanitize_project_id("a")
        assert got == "a"

    def test_max_length_64(self) -> None:
        """64-char input is the maximum length; should be accepted."""
        v = "a" * 64
        got = sanitize_project_id(v)
        assert got == v


class TestSanitizeProjectIdPathTraversal:
    """The whole point of the sanitiser: block path-traversal attempts."""

    def test_blocks_parent_directory(self) -> None:
        got = sanitize_project_id("../etc/passwd")
        assert got is None

    def test_blocks_absolute_path(self) -> None:
        got = sanitize_project_id("/etc/passwd")
        assert got is None

    def test_blocks_relative_path_with_dot(self) -> None:
        got = sanitize_project_id("./project")
        assert got is None

    def test_blocks_windows_path_separator(self) -> None:
        got = sanitize_project_id("project\\name")
        assert got is None

    def test_blocks_trailing_slash(self) -> None:
        got = sanitize_project_id("project/")
        assert got is None

    def test_blocks_leading_slash(self) -> None:
        got = sanitize_project_id("/project")
        assert got is None

    def test_blocks_single_dot(self) -> None:
        """A bare `.` is the current-directory marker — refuse."""
        got = sanitize_project_id(".")
        assert got is None

    def test_blocks_double_dot(self) -> None:
        """A bare `..` is the parent-directory marker — refuse."""
        got = sanitize_project_id("..")
        assert got is None

    def test_blocks_dotfile(self) -> None:
        """Dotfiles aren't legal project slugs."""
        got = sanitize_project_id(".hidden")
        assert got is None


class TestSanitizeProjectIdControlChars:
    """Block control chars, NUL, whitespace, exotic unicode."""

    def test_blocks_nul_byte(self) -> None:
        got = sanitize_project_id("project\x00malicious")
        assert got is None

    def test_blocks_newline(self) -> None:
        got = sanitize_project_id("project\nname")
        assert got is None

    def test_blocks_carriage_return(self) -> None:
        got = sanitize_project_id("project\rname")
        assert got is None

    def test_blocks_tab(self) -> None:
        got = sanitize_project_id("project\tname")
        assert got is None

    def test_blocks_leading_space(self) -> None:
        got = sanitize_project_id(" project")
        assert got is None

    def test_blocks_internal_space(self) -> None:
        """No spaces anywhere — slugs use dashes."""
        got = sanitize_project_id("project name")
        assert got is None

    def test_blocks_trailing_space(self) -> None:
        got = sanitize_project_id("project ")
        assert got is None

    def test_blocks_unicode_letter(self) -> None:
        """ASCII only — even valid unicode like Italian accents → reject."""
        got = sanitize_project_id("progetto-università")
        assert got is None

    def test_blocks_emoji(self) -> None:
        got = sanitize_project_id("project-🚀")
        assert got is None


class TestSanitizeProjectIdLengthBounds:
    """Length cap blocks pathologically-long inputs."""

    def test_blocks_65_chars(self) -> None:
        """Length cap is 64; 65 should reject."""
        got = sanitize_project_id("a" * 65)
        assert got is None

    def test_blocks_very_long(self) -> None:
        """1 KB → reject (defense against memory-amplification)."""
        got = sanitize_project_id("a" * 1024)
        assert got is None


class TestSanitizeProjectIdNoneAndEmpty:
    """None / empty / non-string inputs all return None."""

    def test_returns_none_on_none_input(self) -> None:
        got = sanitize_project_id(None)
        assert got is None

    def test_returns_none_on_empty_string(self) -> None:
        got = sanitize_project_id("")
        assert got is None

    def test_returns_none_on_non_string(self) -> None:
        """Defensive: callers might pass an int (project_id from DB row)."""
        got = sanitize_project_id(42)  # type: ignore[arg-type]
        assert got is None

    def test_returns_none_on_bytes(self) -> None:
        """Bytes (raw header value) should not be accepted as-is."""
        got = sanitize_project_id(b"valid-slug")  # type: ignore[arg-type]
        assert got is None


class TestSanitizeProjectIdSqlInjection:
    """Pin that SQL-injection-shaped strings reject — the container
    routes the value into filesystem paths, but the sanitiser is a
    universal-safe-set guard so adjacent attack surfaces are also
    protected."""

    def test_blocks_semicolon(self) -> None:
        got = sanitize_project_id("project;DROP TABLE")
        assert got is None

    def test_blocks_single_quote(self) -> None:
        got = sanitize_project_id("project'OR'1'='1")
        assert got is None

    def test_blocks_double_quote(self) -> None:
        got = sanitize_project_id('project"name')
        assert got is None


class TestSanitizeProjectIdCommandInjection:
    """Pin command-injection-shaped strings — same generalised guard."""

    def test_blocks_shell_pipe(self) -> None:
        got = sanitize_project_id("project|rm -rf /")
        assert got is None

    def test_blocks_shell_backtick(self) -> None:
        got = sanitize_project_id("project`whoami`")
        assert got is None

    def test_blocks_dollar_sign(self) -> None:
        got = sanitize_project_id("project$(whoami)")
        assert got is None

    def test_blocks_ampersand(self) -> None:
        got = sanitize_project_id("project&&command")
        assert got is None
