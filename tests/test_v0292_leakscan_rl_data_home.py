# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 register item 28 — the RL corpus must not be written under ``~/.claude``.

``claude_mcp_servers/rl_client/rl_logger.py`` used to declare::

    DEFAULT_DIR: Path = Path.home() / ".claude" / "retrieval_rl_data"
    DEFAULT_PATH: Path = DEFAULT_DIR / "rl_events.jsonl"

in the CLASS BODY, and ``RLDataLogger.__init__`` ``mkdir(parents=True)``s
``self._path.parent``. Two defects in one line, both measured before the fix:

1. **Wrong root.** The 2026-08-29 directive is that VCO writes nothing under
   ``~/.claude`` that the harness did not ask for. The metrics streams moved out
   for that reason; this was the call site that lane missed, and
   ``~/.claude/retrieval_rl_data`` exists on real machines.
2. **Unsteerable, and frozen at import.** With ``VCT_CLAUDE_DIR`` *and*
   ``VCT_STATE_DIR`` both set to temp dirs, it still resolved to the real
   ``~/.claude/retrieval_rl_data``. Because a class body runs once, a redirect
   established after import moved nothing — so a guard written against the
   frozen value would have passed while the leak continued.

The fix resolves lazily through :func:`vco_lib.paths.vct_root_dir`. These tests
pin all of it, and three of them fail against the pre-fix source (noted per
test). The pre-existing corpus is deliberately NOT migrated; see
``test_leaves_a_pre_existing_legacy_archive_untouched`` for the leave-alone half
and ``default_rl_data_dir``'s docstring for why copying would be wrong here even
though :mod:`vco_lib.metrics_migration` copies.
"""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import os
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath
from unittest import mock

import pytest

from claude_mcp_servers.rl_client import rl_logger as rl_logger_mod
from claude_mcp_servers.rl_client.rl_logger import (
    RLDataLogger,
    default_rl_data_dir,
    default_rl_log_path,
)
from vco_lib.paths import claude_user_dir, vct_root_dir

RL_LOGGER_SRC = Path(rl_logger_mod.__file__).resolve()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# The resolved location
# ---------------------------------------------------------------------------


def test_default_dir_is_under_the_vct_state_root_not_claude(tmp_path: Path):
    """FAILS pre-fix: the default landed under ``<claude_user_dir()>``."""
    state = tmp_path / "state"
    claude = tmp_path / "claude"
    with mock.patch.dict(
        os.environ,
        {"VCT_STATE_DIR": str(state), "VCT_CLAUDE_DIR": str(claude)},
    ):
        resolved = default_rl_data_dir()

        assert resolved == state / "retrieval_rl_data"
        assert resolved.parent == vct_root_dir()
        assert claude not in resolved.parents, (
            "the RL corpus must not be rooted at ~/.claude — that directory "
            "belongs to Claude Code (2026-08-29 directive)"
        )


def test_default_log_path_keeps_its_filename(tmp_path: Path):
    """The MOVE must not also rename the sink; readers address it by name."""
    with mock.patch.dict(os.environ, {"VCT_STATE_DIR": str(tmp_path)}):
        assert default_rl_log_path() == tmp_path / "retrieval_rl_data" / "rl_events.jsonl"
        assert default_rl_log_path().parent == default_rl_data_dir()


def test_class_attributes_delegate_to_the_module_resolvers(tmp_path: Path):
    """``RLDataLogger.DEFAULT_DIR`` / ``DEFAULT_PATH`` are the same answer.

    Owner access AND instance access, because the pre-fix code was read both
    ways (``self.DEFAULT_PATH`` in ``__init__``, ``RLDataLogger.DEFAULT_DIR``
    from outside) and a descriptor that only served one would be a half-fix.
    """
    with mock.patch.dict(os.environ, {"VCT_STATE_DIR": str(tmp_path)}):
        assert RLDataLogger.DEFAULT_DIR == default_rl_data_dir()
        assert RLDataLogger.DEFAULT_PATH == default_rl_log_path()

        instance = RLDataLogger(log_path=tmp_path / "explicit.jsonl")
        assert instance.DEFAULT_DIR == default_rl_data_dir()
        assert instance.DEFAULT_PATH == default_rl_log_path()


def test_default_dir_follows_a_redirect_set_after_import(tmp_path: Path):
    """FAILS pre-fix. THE regression test for the class-attribute freeze.

    ``rl_logger`` is imported at module scope by this very file, so by the time
    this body runs the import is long done. A class-body value would still be
    pointing at the real home; only a lazy resolution can follow.
    """
    first = tmp_path / "root-one"
    second = tmp_path / "root-two"

    with mock.patch.dict(os.environ, {"VCT_STATE_DIR": str(first)}):
        assert RLDataLogger.DEFAULT_DIR == first / "retrieval_rl_data"
    with mock.patch.dict(os.environ, {"VCT_STATE_DIR": str(second)}):
        assert RLDataLogger.DEFAULT_DIR == second / "retrieval_rl_data"


def test_constructing_the_default_logger_writes_only_under_the_redirect(
    tmp_path: Path,
):
    """FAILS pre-fix: this ``mkdir``'d ``<real home>/.claude/retrieval_rl_data``.

    The one test that exercises the actual side effect — ``__init__`` creates
    the parent directory — with no argument at all, which is the shape that made
    the leak reachable.
    """
    state = tmp_path / "state"
    claude = tmp_path / "claude"
    with mock.patch.dict(
        os.environ,
        {"VCT_STATE_DIR": str(state), "VCT_CLAUDE_DIR": str(claude)},
    ):
        logger = RLDataLogger()

        assert logger.log_path == state / "retrieval_rl_data" / "rl_events.jsonl"
        assert (state / "retrieval_rl_data").is_dir()
        assert not claude.exists(), (
            "constructing the default logger created something under the "
            "~/.claude redirect"
        )


# ---------------------------------------------------------------------------
# Leave-alone: a machine that already has the legacy corpus
# ---------------------------------------------------------------------------


def test_leaves_a_pre_existing_legacy_archive_untouched(tmp_path: Path):
    """LEAVE-ALONE. The already-damaged axis: the old corpus is not migrated.

    Unlike :mod:`vco_lib.metrics_migration`, nothing here copies, moves, reads
    or deletes ``<claude_user_dir()>/retrieval_rl_data``. Proven by hash, not by
    inspection: the seeded archive's bytes and its ``st_mtime_ns`` are identical
    after a default-path logger has been constructed AND written to.
    """
    state = tmp_path / "state"
    claude = tmp_path / "claude"
    archive = claude / "retrieval_rl_data"
    archive.mkdir(parents=True)
    legacy_file = archive / "rl_events.jsonl"
    legacy_file.write_text('{"event": "retrieval", "ts": "old"}\n', encoding="utf-8")

    before_hash = _sha256(legacy_file)
    before_mtime = legacy_file.stat().st_mtime_ns
    before_listing = sorted(p.name for p in archive.iterdir())

    with mock.patch.dict(
        os.environ,
        {"VCT_STATE_DIR": str(state), "VCT_CLAUDE_DIR": str(claude)},
    ):
        assert claude_user_dir() == claude  # the redirect is actually in force
        logger = RLDataLogger(project="P")
        logger.log_retrieval(
            task_id="t1", task_type="implementation", query="q", nodes=[],
        )

    assert _sha256(legacy_file) == before_hash
    assert legacy_file.stat().st_mtime_ns == before_mtime
    assert sorted(p.name for p in archive.iterdir()) == before_listing
    # ...and the event went to the NEW home instead.
    new_sink = state / "retrieval_rl_data" / "rl_events.jsonl"
    assert new_sink.is_file() and new_sink.read_text(encoding="utf-8").strip()


# ---------------------------------------------------------------------------
# Tri-OS: the SHAPE of the decision on Windows / macOS / Linux (R12 / R14)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "os_name,root",
    [
        ("linux", "/home/u/.vct"),
        ("macos", "/Users/u/Library/Application Support/vct"),
        ("windows", "C:/Users/u/AppData/Local/vct"),
    ],
)
def test_shape_is_one_component_joined_onto_whatever_root_resolves(
    os_name: str, root: str,
):
    """The resolver joins ONE component onto ``vct_root_dir()``, on all three.

    The per-OS branches for ``vct_root_dir`` are the X1 batch's job (today every
    OS lands on ``~/.vct``); what this pins is that nothing HERE re-derives a
    home, expands ``~``, or splices a separator — so those branches are picked
    up for free whenever they land. Roots are written with forward slashes so
    ``Path`` parses all three on a POSIX runner; the assertion is about the
    join, which is what this module decides.
    """
    with mock.patch.dict(os.environ, {"VCT_STATE_DIR": root}):
        resolved = default_rl_data_dir()

    assert resolved.name == "retrieval_rl_data", os_name
    assert resolved.parent == Path(root), os_name
    assert resolved.parts[:-1] == Path(root).parts, os_name


def test_source_contains_no_home_reconstruction_in_executable_code():
    """Static anti-regression: the shape cannot come back by hand.

    A ``Path.home()`` / ``expanduser`` in this module is exactly the defect —
    a path with no root, which no override can steer.

    Tokenised rather than line-matched, because the module's docstrings QUOTE
    the old expression on purpose (explaining what went wrong is the point) and
    a "does this line start a docstring" heuristic only ever catches the FIRST
    line of a block. The tokenizer knows what is a string and what is code; a
    prose mention can never trip it, and a real one always will.
    """
    import tokenize

    with tokenize.open(RL_LOGGER_SRC) as handle:
        code_tokens = [
            tok
            for tok in tokenize.generate_tokens(handle.readline)
            if tok.type not in (tokenize.STRING, tokenize.COMMENT)
        ]
    text_by_line: dict[int, str] = {}
    for tok in code_tokens:
        text_by_line.setdefault(tok.start[0], "")
        text_by_line[tok.start[0]] += tok.string

    offenders = [
        (lineno, text)
        for lineno, text in sorted(text_by_line.items())
        if "Path.home()" in text or "expanduser" in text
    ]
    assert not offenders, (
        "rl_logger.py must resolve paths through vco_lib.paths, never "
        f"Path.home()/expanduser: {offenders}"
    )


# ---------------------------------------------------------------------------
# The vendored-standalone contract (the file ships inside a container image)
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _standalone_copy_without_vco_lib():
    """Execute rl_logger.py fresh with ``vco_lib`` unimportable.

    Loaded under a private module name so nothing in ``sys.modules`` that other
    tests hold a reference to is disturbed. ``sys.modules[name] = None`` is the
    stdlib's own "this import fails" sentinel.
    """
    name = "_rl_logger_standalone_probe"
    spec = importlib.util.spec_from_file_location(name, RL_LOGGER_SRC)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    blocked = {"vco_lib": None, "vco_lib.paths": None, name: module}
    with mock.patch.dict(sys.modules, blocked):
        spec.loader.exec_module(module)
        yield module


def test_module_imports_and_logs_with_an_explicit_path_without_vco_lib(
    tmp_path: Path,
):
    """The vendored copy runs in a container image that has no ``vco_lib``.

    ``paid-modules/vct-rl-reranker/rl_logger.py`` must stay byte-identical to
    this file (``tests/test_vendored_file_sync.py``) and that copy exists
    *solely because the container image must build standalone*. So the
    ``vco_lib`` import lives INSIDE the resolver: importing the module, the
    serialization helpers, and an explicitly-pathed logger all keep working
    with no ``vco_lib`` present. Only taking the default needs it.
    """
    with _standalone_copy_without_vco_lib() as module:
        sink = tmp_path / "explicit.jsonl"
        logger = module.RLDataLogger(log_path=sink, project="P")
        logger.log_retrieval(task_id="t", task_type="x", query="q", nodes=[])

        assert sink.is_file()
        assert module.RLDataLogger.SCHEMA_VERSION == RLDataLogger.SCHEMA_VERSION
        assert module.serialize_node_record({"title": "T", "score": 0.5}) == {
            "title": "T", "score": 0.5, "tier": "top_k",
        }


def test_taking_the_default_without_vco_lib_fails_loudly():
    """No silent guess. A broken/absent ``vco_lib`` must be visible.

    CLAUDE.md: *loud-fail, never silent-fallback, on vco_lib imports* — an
    inline ``~/.vct`` reconstruction here would be a second resolver that no
    redirect steers, i.e. the very defect being fixed, reintroduced as its own
    fallback.
    """
    with _standalone_copy_without_vco_lib() as module:
        with pytest.raises(ImportError):
            module.default_rl_data_dir()


# ---------------------------------------------------------------------------
# The conftest guard itself must be able to FAIL
# ---------------------------------------------------------------------------


def test_conftest_guard_rejects_the_pre_fix_value():
    """RED-PROOF of the guard. Feed it exactly what the bug produced.

    A guard nobody has watched fail is a guard nobody can trust — and this
    cycle already found a test whose isolation claim expired silently while it
    kept passing.
    """
    from tests.conftest import (
        RealRlDataHomeNotRedirected,
        assert_rl_data_home_is_redirected,
    )

    pre_fix_value = Path.home() / ".claude" / "retrieval_rl_data"
    with pytest.raises(RealRlDataHomeNotRedirected) as excinfo:
        assert_rl_data_home_is_redirected(pre_fix_value)
    assert "retrieval_rl_data" in str(excinfo.value)
    assert "vct_root_dir" in str(excinfo.value)


def test_conftest_guard_also_rejects_a_plain_vct_root_outside_the_redirect():
    """Right ROOT, wrong machine-state: still refused.

    Guards against a "fix" that moves the literal to ``~/.vct`` inline. That
    would satisfy the directive about ``~/.claude`` and still write the
    maintainer's real home during the suite.
    """
    from tests.conftest import (
        RealRlDataHomeNotRedirected,
        assert_rl_data_home_is_redirected,
    )

    with pytest.raises(RealRlDataHomeNotRedirected):
        assert_rl_data_home_is_redirected(
            Path.home() / ".vct" / "retrieval_rl_data"
        )


def test_conftest_guard_accepts_the_live_resolved_value():
    """LEAVE-ALONE half of the guard: it does not fire on the shipped code.

    Runs with the suite's own redirect in force, i.e. the state every other
    test sees.
    """
    from tests.conftest import assert_rl_data_home_is_redirected

    assert_rl_data_home_is_redirected(RLDataLogger.DEFAULT_DIR)
    assert_rl_data_home_is_redirected(RLDataLogger.DEFAULT_PATH.parent)


def test_pure_posix_and_windows_containment_shapes_agree():
    """The guard's containment test is separator-agnostic (R12 / R14).

    ``_is_inside`` is pure ``Path`` parent arithmetic, so the same decision
    holds for a POSIX root and a Windows root; assert both explicitly rather
    than inferring it from the Linux runner.
    """
    from tests.conftest import _is_inside

    posix_root = PurePosixPath("/tmp/redirect/vct_root")
    win_root = PureWindowsPath(r"C:\Users\u\redirect\vct_root")
    for root, inside, outside in (
        (posix_root, posix_root / "retrieval_rl_data", PurePosixPath("/home/u/.claude")),
        (win_root, win_root / "retrieval_rl_data", PureWindowsPath(r"C:\Users\u\.claude")),
    ):
        assert _is_inside(root, inside)  # type: ignore[arg-type]
        assert _is_inside(root, root)  # type: ignore[arg-type]
        assert not _is_inside(root, outside)  # type: ignore[arg-type]
