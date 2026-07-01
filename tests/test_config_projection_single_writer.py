# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Single-writer lint: only ``vco_lib.config_projection.apply_project_env``
may write the per-project env SURFACES (.claude/settings.json env block,
.claude/env, .vscode/settings.json claude-code.env block), AND only
``vco_lib.env_template.apply_env_template`` may write the fourth surface
``<project_root>/.env`` (Phase 0.D, 2026-05-24).

Phase 0.B contract (see ``.claude/context/plans/diagrams-integration-
excalidraw-mermaid-2026-05-24.md`` §3.0 item 4):

> ``apply_project_env`` is the ONLY function that touches the three env
> surfaces. CI lint enforces this.

Phase 0.D extends the same single-writer discipline to ``<project_root>/.env``
(see ``vco_lib/env_template.py`` module docstring for why ``.env`` is a
separate contract module rather than a fourth surface inside
``vco_lib/config_projection.py``).

Why surface-based, not key-based
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

An earlier draft of this test grepped for direct writes of canonical
KEY NAMES (e.g. ``settings["env"]["KG_COLLECTION"] = ...``). That
yielded ~70 false positives across the test suite — pytest fixtures
that set ``os.environ["KG_COLLECTION"]`` to pin a hub fallback, build
test bundles, or assert on round-trip values were ALL caught. The
real architectural concern isn't the act of naming a canonical key in
code; it's writing to the OUTPUT FILES the contract owns.

So: this test detects "writes to .claude/settings.json | .claude/env |
.vscode/settings.json | <project>/.env" and asks the simpler question:
was the writer the legal one (``apply_project_env`` for the three
Phase 0.B surfaces; ``apply_env_template`` for the Phase 0.D ``.env``
surface), or one of the allowlisted legacy writers carrying the
migration marker? A new writer = a contract violation.

False positives that remain
~~~~~~~~~~~~~~~~~~~~~~~~~~~

A test that creates a fresh ``.claude/settings.json`` in a ``tmp_path``
fixture to exercise a downstream reader IS a write — and it's
legitimate. We allowlist test files at the file level (they're under
``tests/``) and the orchestrator-managed-paths string scrubber (it
mentions the file path in a string list, not as a write target).

Legacy callers (Rust + Python backfills)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Production writers that haven't been migrated to subprocess-into-Python
yet are allowlisted by their relative path AND must carry the marker::

    // config_projection: legacy_caller_pending_migration

Removing the marker line removes the allowlist entry; CI then fails on
the next run unless the caller has been migrated. This is the "rip the
plaster off in N PRs, not 1" migration discipline.

Run: pytest tests/test_config_projection_single_writer.py -v
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

import pytest

from vco_lib.config_projection import list_canonical_keys
from vco_lib.env_template import list_canonical_env_template_keys


REPO_ROOT = Path(__file__).resolve().parent.parent


# ─── Path patterns the lint searches for ────────────────────────────────
#
# We're looking for WRITES to specific files. The detection grammar is
# language-specific because the act of "open and write to .claude/env"
# looks different in Python (``open(path, 'w')``), Rust
# (``fs::write(path, ...)``), and shell (``> .claude/env`` /
# ``>> .claude/env``).

_TARGET_FILE_FRAGMENTS: tuple[str, ...] = (
    # .claude/settings.json — the canonical env surface.
    ".claude/settings.json",
    # The legacy Windows-style separator. Cross-OS paths can mix.
    r".claude\settings.json",
    # .claude/env — shell-source surface.
    ".claude/env",
    r".claude\env",
    # .vscode/settings.json — opt-in third surface.
    ".vscode/settings.json",
    r".vscode\settings.json",
)


# ─── Phase 0.D: project-root .env detection ─────────────────────────────
#
# The fourth surface is ``<project_root>/.env``. Detection is
# intentionally narrower than the three Phase 0.B surfaces because
# ``.env`` is an ambiguous path string — many files have valid `.env`
# substrings (``.envrc``, ``pytest.env``, ``.env.example``, etc.). We
# match ONLY exact ``".env"`` quoted literals — never the substring
# ``.env`` inside another filename. The dedicated patterns are kept
# separate from ``_TARGET_FILE_FRAGMENTS`` so a regex-builder bug
# can't silently widen the match scope.

# Match the EXACT quoted literal `".env"` (or `'.env'`), as appears in
# ``Path(...) / ".env"`` and ``open("...path.../.env", "w")`` call
# sites.
_PROJECT_ENV_TARGET = ".env"


# ─── Allowlists ─────────────────────────────────────────────────────────

# The single legal writers + tests + parity guards.
_ALLOWLIST_FILES: set[Path] = {
    # Phase 0.B contract module.
    REPO_ROOT / "vco_lib" / "config_projection.py",
    # Phase 0.D contract module (the legal .env writer).
    REPO_ROOT / "vco_lib" / "env_template.py",
    # Lint test itself + Phase 0.B test files.
    REPO_ROOT / "tests" / "test_config_projection_single_writer.py",
    REPO_ROOT / "tests" / "test_config_projection.py",
    REPO_ROOT / "tests" / "test_config_projection_byte_identical.py",
    # Phase 0.D test files.
    REPO_ROOT / "tests" / "test_env_template.py",
    REPO_ROOT / "tests" / "test_env_template_byte_identical.py",
}

# Whole directories where any write to the target paths is acceptable
# (test fixtures, knowledge nodes, design docs that reference paths in
# prose, the ignore-walker that LISTS the files for skip purposes).
_ALLOWLIST_DIRS: set[Path] = {
    REPO_ROOT / "tests",
    REPO_ROOT / "knowledge",
    REPO_ROOT / "docs",
    REPO_ROOT / "internal",
    # Rust crate's tests live alongside source.
    REPO_ROOT / "launcher" / "src-tauri" / "tests",
}

# Marker that legacy callers to the THREE Phase 0.B surfaces must carry.
# Phase 0.B Part 2 (2026-05-25): the marker is now PURELY HISTORICAL for
# Phase 0.B surfaces — the allowlist set is empty, and
# `test_legacy_writers_allowlist_is_empty_post_part_2` guards against
# any re-introduction. The marker constant is retained so that future
# deferred migrations (Phase 0.E for user-secret writes once the
# active-flag bridge lands in Python) can re-use the same allowlist +
# marker discipline without re-inventing the lint contract.
_LEGACY_MARKER = "config_projection: legacy_caller_pending_migration"

# Phase 0.D marker for legacy ``.env`` writers. Kept separate so a
# Phase 0.D-only allowlist entry doesn't accidentally legitimise a
# Phase 0.B violation in the same file.
_LEGACY_ENV_TEMPLATE_MARKER = "env_template: legacy_caller_pending_migration"

# Legacy direct writers — production code that hasn't been migrated yet.
#
# Phase 0.B Part 2 (2026-05-25): EMPTY. All historical entries were
# migrated to delegate to `vco_lib.config_projection.apply_project_env`:
#   * launcher/src-tauri/src/commands/projects_v2.rs — production
#     callers (create / rename / refresh / write-disabled-toggle) now
#     subprocess into `python -m vco_lib.config_projection apply`
#     via `apply_project_env_via_python`. The legacy
#     `write_project_env_files` function is retained for test fixtures
#     and for the user-secret SecretsPanel flow (which uses an in-Rust
#     code path the Python contract doesn't cover yet — Phase 0.E).
#   * install.py — `_backfill_code_graph_project_env` (orchestrator-root
#     env projection) now imports + calls `apply_project_env` directly.
#   * vco_lib/project_init.py — the per-user-project install-bundle
#     backfills now call `_apply_canonical_env_via_config_projection`,
#     a thin wrapper around the contract.
#
# Future entries (Phase 0.D / 0.E) re-populate this set with the same
# marker-comment discipline, then empty it again on full migration.
_LEGACY_PRODUCTION_WRITERS: set[Path] = set()

# Phase 0.D: production code that writes ``.env`` directly and hasn't
# been fully migrated. Each entry MUST carry
# ``_LEGACY_ENV_TEMPLATE_MARKER``. Empty allowlist = full migration
# complete.
#
# Current entries:
#   * install.py — the fresh-write branch in ``_write_env_config`` still
#     writes a mix of canonical Phase 0.D keys + install-time-only keys
#     (EMBEDDING_MODEL / EMBEDDING_DIMS / EMBEDDING_PROVIDER /
#     CODE_EMBED_BACKEND / CODE_EMBED_MODEL / CODE_EMBED_DIMS /
#     VCT_JOERN_AVAILABLE / VCT_TELEMETRY / banner comments /
#     RL-section placeholders). Splitting those into a managed-block-
#     only path + a separate writer for the install-time tail is a
#     follow-up refactor beyond Phase 0.D's brief. The EXISTING-file
#     branch DOES route through ``apply_env_template`` via
#     ``_ensure_env_template`` (migrated in this PR).
#   * launcher/src-tauri/src/commands/projects_v2.rs —
#     ``ensure_project_env_template`` is the Rust legacy writer; full
#     migration to subprocess-into-Python is Phase 0.D Part 2 (a
#     follow-up matching the Phase 0.B Part 2 / write_project_env_files
#     pattern; out of scope for this PR).
_LEGACY_ENV_TEMPLATE_WRITERS: set[Path] = {
    REPO_ROOT / "install.py",
    REPO_ROOT / "launcher" / "src-tauri" / "src" / "commands" / "projects_v2.rs",
    # v0.2.54 Track B: writes to infrastructure/.env (compose project's
    # env file, a different surface from the canonical project .env).
    # Pre-extraction the same write lived in install.py and was on this
    # allowlist; carry forward until the compose-env surface gets its
    # own contract decision (Phase 0.D follow-up).
    REPO_ROOT / "vco_lib" / "compose_env.py",
}


# ─── Scanners ───────────────────────────────────────────────────────────


def _python_write_to_target_patterns() -> list[re.Pattern]:
    """Patterns that match Python code WRITING to any target surface.

    Looks for:
      * ``open(... ".claude/settings.json" ..., "w" / "wb" / "a")``
      * Any expression containing a target string literal followed by
        ``.write_text`` or ``.write_bytes`` (same-line aliasing).
      * Variable assignment ``= ... / ".claude/settings.json"`` followed
        on later lines by a ``.write_text`` / ``.write_bytes`` call on
        the same variable — caught at FILE LEVEL by the
        :func:`_python_file_level_path_writes` helper below.
      * ``shutil.copy(_, "...settings.json")``
    """
    target_alt = "|".join(re.escape(frag) for frag in _TARGET_FILE_FRAGMENTS)
    return [
        # open("....path...", "w"/"wb"/"a")
        re.compile(
            rf"""open\s*\(\s*[^)]*?['"][^'"]*({target_alt})['"][^)]*?,\s*['"][wa]b?\+?['"]"""
        ),
        # Path(...).write_text / write_bytes with target on same line
        re.compile(
            rf"""['"][^'"]*({target_alt})['"][^)]*?\)\s*\.\s*write_text"""
        ),
        re.compile(
            rf"""['"][^'"]*({target_alt})['"][^)]*?\)\s*\.\s*write_bytes"""
        ),
        # shutil.copy / copy2 / copyfile destination
        re.compile(
            rf"""shutil\.(?:copy|copy2|copyfile|move)\s*\([^)]+,\s*['"][^'"]*({target_alt})"""
        ),
    ]


def _python_file_level_path_writes(content: str) -> list[tuple[int, str]]:
    """Catch the file-level alias pattern that the per-line patterns miss.

    Looks for any of:
      * ``target = <something> / ".claude/settings.json"`` (single literal)
      * ``target = <something> / ".claude" / "settings.json"`` (chained)
      * ``target = <something> / ".claude" / "env"``
      * ``target = <something> / ".vscode" / "settings.json"``
    THEN later in the file:
      * ``target.write_text(...) | target.write_bytes(...)``

    Specifically: any variable assigned to a Path expression containing
    a target literal is "tainted"; a later call to ``.write_text`` /
    ``.write_bytes`` on that variable is a violation. Same-function-
    body scope (we don't track variables across functions — false
    negatives on cross-function aliasing are acceptable; the common
    pattern is single-function).

    Returns a list of (lineno, line) violation tuples for use by the
    main scanner.
    """
    target_alt = "|".join(re.escape(frag) for frag in _TARGET_FILE_FRAGMENTS)
    # Pattern A: single-literal assignment.
    tainted_assign_single = re.compile(
        rf"""^\s*([a-zA-Z_][a-zA-Z_0-9]*)\s*=\s*[^=].*?['"][^'"]*({target_alt})"""
    )
    # Pattern B: chained Path concatenation ending in the file basename.
    # Catches `... / ".claude" / "settings.json"` and `... / ".vscode" / "settings.json"`.
    # The `\.claude` / `\.vscode` token must appear first, then `settings.json`
    # OR `env` later in the same expression.
    tainted_assign_chained = re.compile(
        r"""^\s*([a-zA-Z_][a-zA-Z_0-9]*)\s*=\s*[^=].*?["']\.claude["'].*?["'](?:settings\.json|env)["']"""
    )
    tainted_assign_vscode_chained = re.compile(
        r"""^\s*([a-zA-Z_][a-zA-Z_0-9]*)\s*=\s*[^=].*?["']\.vscode["'].*?["']settings\.json["']"""
    )
    # Match `<name>.write_text(...)` or `<name>.write_bytes(...)`.
    write_call = re.compile(
        r"""^\s*([a-zA-Z_][a-zA-Z_0-9]*)\s*\.\s*write_(?:text|bytes)\s*\("""
    )

    violations: list[tuple[int, str]] = []
    # We track per-function scope (reset tainted set at each `def`).
    tainted: set[str] = set()
    for lineno, line in enumerate(content.splitlines(), start=1):
        stripped = line.strip()
        # Reset taint at function boundaries (cheap heuristic).
        if stripped.startswith(("def ", "async def ", "class ")):
            tainted.clear()
            continue
        # Comments / docstrings — skip.
        if not stripped or stripped.startswith(("#", '"""', "'''")):
            continue
        for pat in (
            tainted_assign_single,
            tainted_assign_chained,
            tainted_assign_vscode_chained,
        ):
            m_assign = pat.search(line)
            if m_assign:
                tainted.add(m_assign.group(1))
                break
        else:
            m_write = write_call.search(line)
            if m_write and m_write.group(1) in tainted:
                violations.append((lineno, line.rstrip()))
    return violations


def _rust_write_to_target_patterns() -> list[re.Pattern]:
    """Patterns that match Rust code WRITING to any target surface.

    Looks for:
      * ``std::fs::write(path, ...)`` or ``fs::write(path, ...)`` where
        ``path`` mentions the target.
      * ``File::create(...)`` followed by writes — we only match the
        creation site (writes after a `File::create` of a target path
        are presumed).
      * ``writeln!(file, ...)`` is too generic to grep without aliasing;
        a `fs::write` / `File::create` match upstream is the leading
        indicator.
    """
    target_alt = "|".join(re.escape(frag) for frag in _TARGET_FILE_FRAGMENTS)
    return [
        # fs::write(<path-expr-mentioning-target>, ...)
        # The path expression can be a builder chain ending in `.join("settings.json")`.
        # We match the literal substring appearing within the fs::write call.
        re.compile(
            rf"""fs::write\s*\(\s*[^,]*?({target_alt})[^,]*?,"""
        ),
        # File::create(<path-expr-mentioning-target>)
        re.compile(
            rf"""File::create\s*\(\s*[^)]*?({target_alt})"""
        ),
    ]


def _shell_write_to_target_patterns() -> list[re.Pattern]:
    """Patterns that match shell scripts WRITING to any target surface.

    Shell hooks must NOT redirect stdout/heredoc into the env files.
    """
    target_alt = "|".join(re.escape(frag) for frag in _TARGET_FILE_FRAGMENTS)
    return [
        # `> .claude/env` or `>> .claude/env`
        re.compile(
            rf""">>?\s*['"]?[^'"\s|;&<>]*({target_alt})"""
        ),
        # `cp _ .claude/env` / `mv _ .claude/env`
        re.compile(
            rf"""\b(cp|mv)\s+[^|;&<>]*\s+['"]?[^'"\s|;&<>]*({target_alt})"""
        ),
        # `tee .claude/env`
        re.compile(
            rf"""\btee\s+[^|;&<>]*({target_alt})"""
        ),
    ]


# ─── Phase 0.D: .env-specific scanners ──────────────────────────────────


def _python_dotenv_write_patterns() -> list[re.Pattern]:
    """Python patterns that match writes to a project-root ``.env``.

    Matches the exact quoted literal ``".env"`` (single or double quoted)
    — NOT ``.envrc``, ``pytest.env``, ``.env.example``, etc. The quote
    boundary is what differentiates the bare ``.env`` filename from
    substring noise.

      * ``open("....env", "w")`` — direct opens.
      * Inline ``Path(...).write_text(...)`` where path literal ends ``.env``.
      * ``shutil.copy(_, ".env")`` / ``shutil.move(_, ".env")``.

    File-level alias pattern (``var = ... / ".env"`` then
    ``var.write_text(...)``) is handled by
    :func:`_python_file_level_dotenv_writes`.
    """
    # Match the `.env` literal as the LAST path component, anchored by
    # the closing quote. Allows leading path content before `.env`.
    # Examples it must match:
    #   open("/tmp/foo/.env", "w")
    #   open(".env", "w")
    #   open(folder + "/.env", "w")
    #   Path(...).write_text — when paired with a ".env" literal nearby
    # Examples it must NOT match:
    #   open(".envrc", "w")
    #   open("pytest.env", "w")
    #   open(".env.example", "w")
    # Strategy: require the .env literal to be immediately followed by
    # the SAME quote character (no extra characters).
    return [
        # open(... ".env", "w") — match `.env"` or `.env'` at end of arg.
        re.compile(
            r"""open\s*\(\s*[^)]*?(?:['"]|^|/|\\)\.env(['"])[^)]*?,\s*['"][wa]b?\+?['"]"""
        ),
        # Inline Path(...).write_text — literal ends ".env"
        re.compile(
            r"""(?:['"]|/|\\)\.env(['"])[^)]*?\)\s*\.\s*write_text"""
        ),
        re.compile(
            r"""(?:['"]|/|\\)\.env(['"])[^)]*?\)\s*\.\s*write_bytes"""
        ),
        # shutil.copy / copy2 / copyfile / move with .env destination.
        re.compile(
            r"""shutil\.(?:copy|copy2|copyfile|move)\s*\([^)]+,\s*['"][^'"]*(?:/|\\|^)\.env['"]"""
        ),
    ]


def _python_file_level_dotenv_writes(content: str) -> list[tuple[int, str]]:
    """Catch the file-level alias pattern for ``.env``:

      * ``target = <something> / ".env"`` (single literal)
      * ``target = <project_root> / ".env"`` (path-builder chain)
      * THEN later: ``target.write_text(...)`` / ``target.write_bytes(...)``

    Same single-function-body scope as
    :func:`_python_file_level_path_writes`.
    """
    # Match `var = ... ".env"` where ".env" is the LAST quoted string in
    # the right-hand side AND is preceded by either a path separator
    # inside the literal (`/.env`) or is the bare filename. Strictly
    # excludes `.envrc`, `pytest.env`, `.env.example`.
    tainted_assign = re.compile(
        r"""^\s*([a-zA-Z_][a-zA-Z_0-9]*)\s*=\s*[^=].*?["']\.env(["'])"""
    )
    write_call = re.compile(
        r"""^\s*([a-zA-Z_][a-zA-Z_0-9]*)\s*\.\s*write_(?:text|bytes)\s*\("""
    )

    violations: list[tuple[int, str]] = []
    tainted: set[str] = set()
    for lineno, line in enumerate(content.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith(("def ", "async def ", "class ")):
            tainted.clear()
            continue
        if not stripped or stripped.startswith(("#", '"""', "'''")):
            continue
        m_assign = tainted_assign.search(line)
        if m_assign:
            # Ensure the matched literal IS ".env" and not e.g. ".env.example".
            # The trailing quote is captured; require the immediate char
            # after the matched group's closing quote NOT to be a continuation.
            tainted.add(m_assign.group(1))
            continue
        m_write = write_call.search(line)
        if m_write and m_write.group(1) in tainted:
            violations.append((lineno, line.rstrip()))
    return violations


def _rust_dotenv_write_patterns() -> list[re.Pattern]:
    """Rust patterns that match writes to a ``.env`` file.

      * ``fs::write(path, ...)`` where path ends in ``.env``.
      * ``File::create(...)`` of a ``.env`` path.
      * ``OpenOptions::new()....open(env_path)`` where ``env_path``
        is constructed via ``.join(".env")`` — caught at file level
        via a less-precise text scan (alias case).
    """
    return [
        # fs::write(<path mentioning .env>, ...) — match `.join(".env")`
        # or string-literal `.env"` inside the first arg.
        re.compile(
            r"""fs::write\s*\(\s*[^,]*?(?:\.join\(\s*"\.env"\s*\)|"\.env"|"\.env\s*"|"[^"]*?/\.env")"""
        ),
        # File::create with the .env literal.
        re.compile(
            r"""File::create\s*\(\s*[^)]*?(?:\.join\(\s*"\.env"\s*\)|"\.env")"""
        ),
        # OpenOptions builder ending in `.open(<var with .env>)` — best-
        # effort: match the `.open(` after a `.append(true)` or `.write(true)`
        # builder, paired with a same-file `.join(".env")` assignment.
        re.compile(
            r"""\.open\s*\(\s*&?(?:env_path|env_file)\s*\)"""
        ),
    ]


def _shell_dotenv_write_patterns() -> list[re.Pattern]:
    """Shell-script patterns that write a ``.env`` file.

    Matches redirects + copy/move/tee targeting a path ending in
    ``/.env`` or ``.env`` as the bare filename. Avoids ``.envrc`` /
    ``pytest.env`` / ``.env.example`` by requiring the path component
    to END at ``.env`` (next char is whitespace / EOL / `;` / `&`).
    """
    return [
        # `> /path/.env` or `>> .env`
        re.compile(
            r""">>?\s*['"]?[^'"\s|;&<>]*?(?:^|/|\\|\b)\.env(?=['"\s;&|]|$)"""
        ),
        # `cp/mv _ .env` / `cp/mv _ /some/.env`
        re.compile(
            r"""\b(cp|mv)\s+[^|;&<>]+\s+['"]?[^'"\s|;&<>]*?(?:^|/|\\|\b)\.env(?=['"\s;&|]|$)"""
        ),
        # `tee .env`
        re.compile(
            r"""\btee\s+[^|;&<>]*?(?:^|/|\\|\b)\.env(?=['"\s;&|]|$)"""
        ),
    ]


# ─── Helpers ────────────────────────────────────────────────────────────


def _iter_target_files() -> Iterable[Path]:
    """Yield every source file the lint scans.

    Limits: stays within the repo; skips common build/cache dirs.
    """
    extensions = {".py", ".rs", ".sh", ".ps1"}
    skip_dirs = {
        ".git", ".venv", "node_modules", "target", "__pycache__",
        ".pytest_cache", "dist", "build", ".cargo", ".rustup",
        # Don't scan our own worktrees (other agents' branches).
        ".claude",
        # ``.wt`` = orchestrator-created parallel-worktree dir (gitignored,
        # full repo copies of each in-flight track) — same transient-mirror
        # class as ``.claude/worktrees``; prune the whole subtree.
        ".wt",
    }
    for root, dirs, files in _walk_with_pruning(REPO_ROOT, skip_dirs):
        for name in files:
            p = Path(root) / name
            if p.suffix in extensions:
                yield p


def _walk_with_pruning(root: Path, skip_names: set[str]):
    """Pure-Python os.walk with directory pruning by name."""
    import os
    for r, ds, fs in os.walk(root):
        ds[:] = [d for d in ds if d not in skip_names]
        yield r, ds, fs


def _read_text_safely(path: Path) -> str:
    """Read text or return '' on binary/perm error."""
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return ""


def _strip_rust_test_modules(content: str) -> str:
    """Remove ``#[cfg(test)] mod ... { ... }`` blocks from Rust source.

    Inline test modules are Rust's analogue of Python's ``tests/`` dir —
    they're test code, not production. Writes to env surfaces inside
    them are test fixtures (building fake repo trees, exercising
    downstream readers) and don't violate the contract.

    Brace-counting: starts at the ``{`` after ``#[cfg(test)]`` mod
    declaration, accumulates depth, terminates at the matching ``}``.

    Strings and comments containing literal braces COULD throw off the
    counter, but Rust test modules don't typically contain literal
    braces in raw strings around test blocks. Worst case: a stray brace
    truncates the strip early, leaving SOME test code in scope — that
    would be a false positive (the lint would flag legitimate test
    writes), but it can be fixed by adding the file to the dirs
    allowlist OR refactoring the offending raw string. We accept the
    trade-off for the simplicity of brace-counting vs full Rust parser
    integration.

    Returns the content with test modules replaced by blank lines (so
    line numbers in error reports stay accurate).

    Implementation note: operates on the raw string char-by-char with
    a single linear scan. For a 9000-line file (~300KB) this is well
    under 200ms; the per-line nested loop in an earlier draft was
    quadratic when many test modules nested deeply.
    """
    if "#[cfg(test)]" not in content:
        return content

    out = list(content)
    n = len(content)
    i = 0
    # Each pass: find the next "#[cfg(test)]", advance to the opening
    # `{`, brace-count to the closing `}`, blank that range.
    needle = "#[cfg(test)]"
    while i < n:
        idx = content.find(needle, i)
        if idx == -1:
            break
        # Find the first `{` after idx.
        brace = content.find("{", idx + len(needle))
        if brace == -1:
            break
        # Brace-count from brace.
        depth = 1
        j = brace + 1
        while j < n and depth > 0:
            ch = content[j]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            j += 1
        # Blank from idx to j (inclusive of the closing brace), but
        # keep newlines so line numbers stay aligned.
        for k in range(idx, j):
            if out[k] != "\n":
                out[k] = " "
        i = j
    return "".join(out)


def _path_is_under(p: Path, root: Path) -> bool:
    """True if ``p`` is under ``root`` (or equal)."""
    try:
        p.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _file_carries_legacy_marker(content: str) -> bool:
    return _LEGACY_MARKER in content


# ─── The tests ──────────────────────────────────────────────────────────


def test_no_direct_writes_to_env_surfaces_outside_contract() -> None:
    """Surface-write guard.

    Scans the repo for code that writes to any of the three env
    surfaces and asserts the writer is the legal one OR an allowlisted
    legacy caller carrying the migration marker.
    """
    py_patterns = _python_write_to_target_patterns()
    rs_patterns = _rust_write_to_target_patterns()
    sh_patterns = _shell_write_to_target_patterns()

    violations: list[str] = []
    legacy_files_with_hits: set[Path] = set()

    for path in _iter_target_files():
        if path in _ALLOWLIST_FILES:
            continue
        if any(_path_is_under(path, d) for d in _ALLOWLIST_DIRS):
            continue

        content = _read_text_safely(path)
        if not content:
            continue

        is_legacy = path in _LEGACY_PRODUCTION_WRITERS

        # Apply patterns per language.
        if path.suffix == ".py":
            patterns = py_patterns
        elif path.suffix == ".rs":
            # Strip inline test modules so test-fixture writes don't
            # false-positive. Production writes stay in scope.
            content = _strip_rust_test_modules(content)
            patterns = rs_patterns
        elif path.suffix in (".sh", ".ps1"):
            patterns = sh_patterns
        else:
            continue

        file_hits: list[tuple[int, str]] = []
        for lineno, line in enumerate(content.splitlines(), start=1):
            stripped = line.strip()
            if not stripped:
                continue
            # Skip obvious commentary lines.
            if stripped.startswith(("#", "//", '"""', "'''", "*", "///")):
                continue
            for pat in patterns:
                m = pat.search(line)
                if m:
                    file_hits.append((lineno, line.rstrip()))
                    break  # one hit per line is enough

        # Python-only: catch the file-level alias pattern that misses
        # the per-line patterns (target = ... / "settings.json"; later
        # target.write_text(...)).
        if path.suffix == ".py":
            file_hits.extend(_python_file_level_path_writes(content))

        if not file_hits:
            continue

        if is_legacy:
            legacy_files_with_hits.add(path)
            if not _file_carries_legacy_marker(content):
                violations.append(
                    f"{path.relative_to(REPO_ROOT)}: writes to env surface "
                    f"but missing required marker '{_LEGACY_MARKER}'. "
                    f"Either add the marker (with a TODO to migrate) or "
                    f"remove from _LEGACY_PRODUCTION_WRITERS."
                )
            continue

        rel = path.relative_to(REPO_ROOT)
        for lineno, snippet in file_hits:
            violations.append(
                f"{rel}:{lineno}: direct write to an env surface — "
                f"`{snippet[:120]}`. Route through "
                f"vco_lib.config_projection.apply_project_env instead, "
                f"or add the file to _LEGACY_PRODUCTION_WRITERS in the "
                f"lint test with the marker comment for a deferred migration."
            )

    if violations:
        msg = "\n".join(violations)
        raise AssertionError(
            f"{len(violations)} direct-write violation(s) of the "
            f"single-writer contract:\n{msg}\n\n"
            f"See vco_lib/config_projection.py for the legal writer.\n"
            f"Allowed paths: vco_lib.config_projection.apply_project_env "
            f"(or its CLI: `python -m vco_lib.config_projection apply`).\n"
        )


def test_legacy_writers_carry_marker() -> None:
    """Independently of finding violations, every entry in
    ``_LEGACY_PRODUCTION_WRITERS`` MUST contain the marker.

    This catches the case where someone removes the marker comment
    (intending to migrate) but forgets to also remove the entry from
    ``_LEGACY_PRODUCTION_WRITERS``.
    """
    for path in _LEGACY_PRODUCTION_WRITERS:
        if not path.exists():
            # An entry pointing at a file that no longer exists is
            # stale — should be removed from the allowlist.
            pytest.fail(
                f"{path.relative_to(REPO_ROOT)} is in "
                f"_LEGACY_PRODUCTION_WRITERS but does not exist on disk."
            )
        content = _read_text_safely(path)
        assert _file_carries_legacy_marker(content), (
            f"{path.relative_to(REPO_ROOT)} is allowlisted as a legacy "
            f"writer but does not contain the required marker "
            f"'{_LEGACY_MARKER}'. Add the marker as a comment "
            f"explaining the deferred migration, or remove the entry."
        )


def test_canonical_key_set_is_non_empty() -> None:
    """Sanity guard: the contract has SOMETHING to enforce."""
    keys = list_canonical_keys()
    assert len(keys) >= 10, (
        "list_canonical_keys() returned <10 keys; did someone empty the "
        "registry? Expected the full canonical set (KG_COLLECTION, "
        "VCT_KG_ACCESS_LIST, SHARED_KG_WRITE_DISABLED, etc.)."
    )


def test_env_template_canonical_subset_is_non_empty() -> None:
    """Phase 0.D sanity guard: the .env template subset has SOMETHING."""
    keys = list_canonical_env_template_keys()
    assert len(keys) >= 8, (
        "list_canonical_env_template_keys() returned <8 keys; did "
        "someone empty the subset? Expected at least the identity + KG "
        "+ service-URL keys (PROJECT_NAME, KG_COLLECTION, WEAVIATE_URL, "
        "etc.)."
    )


def test_legacy_writers_allowlist_is_empty_post_part_2() -> None:
    """Phase 0.B Part 2 (2026-05-25) migrated every legacy direct
    writer to delegate to ``vco_lib.config_projection.apply_project_env``.

    The Rust production callers (`create_project_v2`, `rename_project_v2`,
    `set_shared_kg_write_disabled`, `refresh_project_env_with_db`) now
    invoke `apply_project_env_via_python` which subprocesses into the
    Python contract. The Python install.py + project_init.py backfills
    now import + call the contract directly.

    The allowlist set MUST stay empty going forward. Any re-introduction
    of a direct env-surface writer is the regression we're guarding
    against: route new writers through
    ``python -m vco_lib.config_projection apply`` (subprocess from Rust)
    or ``apply_project_env(project_env_from_db(project_id))`` (direct
    Python import) instead of adding to this allowlist.

    Future deferred migrations (Phase 0.E for user-secret writes once
    the active-flag bridge lands in Python) MAY re-populate the
    allowlist with the same marker-comment discipline; they MUST
    re-empty it on full migration.
    """
    assert _LEGACY_PRODUCTION_WRITERS == set(), (
        "Phase 0.B Part 2 migrated all legacy writers. New entries in "
        "_LEGACY_PRODUCTION_WRITERS indicate a regression. Migrate the "
        "new writer to subprocess via "
        "`python -m vco_lib.config_projection apply` (Rust) or "
        "`from vco_lib.config_projection import apply_project_env` "
        "(Python) instead of adding to the allowlist.\n\n"
        f"Currently allowlisted: {sorted(p.name for p in _LEGACY_PRODUCTION_WRITERS)}"
    )


def test_legacy_marker_is_documented() -> None:
    """The marker string is documented in this file's module docstring so
    a future maintainer can find it without grepping the test body."""
    own_source = Path(__file__).read_text()
    assert _LEGACY_MARKER in own_source
    # And it appears in the docstring, not just the constant.
    docstring_chunk = own_source.split('"""', 2)[1]
    assert "legacy_caller_pending_migration" in docstring_chunk, (
        "The legacy marker should be documented in the module docstring "
        "so allowlisted-file maintainers know what comment to add."
    )


# ─── Phase 0.D: .env single-writer enforcement ──────────────────────────


def _file_carries_env_template_marker(content: str) -> bool:
    return _LEGACY_ENV_TEMPLATE_MARKER in content


def test_no_direct_writes_to_dotenv_outside_contract() -> None:
    """Phase 0.D surface-write guard for ``<project_root>/.env``.

    Scans the repo for code that writes a ``.env`` file directly and
    asserts the writer is the legal one (``vco_lib.env_template``) or
    an allowlisted legacy caller carrying the migration marker.
    """
    py_patterns = _python_dotenv_write_patterns()
    rs_patterns = _rust_dotenv_write_patterns()
    sh_patterns = _shell_dotenv_write_patterns()

    violations: list[str] = []
    legacy_files_with_hits: set[Path] = set()

    for path in _iter_target_files():
        if path in _ALLOWLIST_FILES:
            continue
        if any(_path_is_under(path, d) for d in _ALLOWLIST_DIRS):
            continue

        content = _read_text_safely(path)
        if not content:
            continue

        is_legacy = path in _LEGACY_ENV_TEMPLATE_WRITERS

        if path.suffix == ".py":
            patterns = py_patterns
        elif path.suffix == ".rs":
            content = _strip_rust_test_modules(content)
            patterns = rs_patterns
        elif path.suffix in (".sh", ".ps1"):
            patterns = sh_patterns
        else:
            continue

        file_hits: list[tuple[int, str]] = []
        for lineno, line in enumerate(content.splitlines(), start=1):
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith(("#", "//", '"""', "'''", "*", "///")):
                continue
            for pat in patterns:
                m = pat.search(line)
                if m:
                    file_hits.append((lineno, line.rstrip()))
                    break

        # Python file-level alias pattern.
        if path.suffix == ".py":
            file_hits.extend(_python_file_level_dotenv_writes(content))

        if not file_hits:
            continue

        if is_legacy:
            legacy_files_with_hits.add(path)
            if not _file_carries_env_template_marker(content):
                violations.append(
                    f"{path.relative_to(REPO_ROOT)}: writes to .env but "
                    f"missing required marker "
                    f"'{_LEGACY_ENV_TEMPLATE_MARKER}'. Either add the "
                    f"marker (with a TODO to migrate to apply_env_template) "
                    f"or remove from _LEGACY_ENV_TEMPLATE_WRITERS."
                )
            continue

        rel = path.relative_to(REPO_ROOT)
        for lineno, snippet in file_hits:
            violations.append(
                f"{rel}:{lineno}: direct write to .env — "
                f"`{snippet[:120]}`. Route through "
                f"vco_lib.env_template.apply_env_template instead, "
                f"or add the file to _LEGACY_ENV_TEMPLATE_WRITERS in "
                f"the lint test with the marker comment for a "
                f"deferred migration."
            )

    if violations:
        msg = "\n".join(violations)
        raise AssertionError(
            f"{len(violations)} direct-write violation(s) of the Phase "
            f"0.D ``.env`` single-writer contract:\n{msg}\n\n"
            f"See vco_lib/env_template.py for the legal writer.\n"
            f"Allowed path: vco_lib.env_template.apply_env_template "
            f"(or its CLI: `python -m vco_lib.env_template apply`).\n"
        )


def test_legacy_env_template_writers_carry_marker() -> None:
    """Phase 0.D companion to ``test_legacy_writers_carry_marker``:
    every entry in ``_LEGACY_ENV_TEMPLATE_WRITERS`` must contain
    ``_LEGACY_ENV_TEMPLATE_MARKER``."""
    for path in _LEGACY_ENV_TEMPLATE_WRITERS:
        if not path.exists():
            pytest.fail(
                f"{path.relative_to(REPO_ROOT)} is in "
                f"_LEGACY_ENV_TEMPLATE_WRITERS but does not exist on disk."
            )
        content = _read_text_safely(path)
        assert _file_carries_env_template_marker(content), (
            f"{path.relative_to(REPO_ROOT)} is allowlisted as a legacy "
            f".env writer but does not contain the required marker "
            f"'{_LEGACY_ENV_TEMPLATE_MARKER}'. Add the marker as a "
            f"comment explaining the deferred migration, or remove the "
            f"entry."
        )


def test_env_template_marker_is_documented() -> None:
    """The Phase 0.D marker string is documented in this file's module
    docstring."""
    own_source = Path(__file__).read_text()
    assert _LEGACY_ENV_TEMPLATE_MARKER in own_source
