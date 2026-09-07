# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 regclean item 2 — the RL JSONL importer's defaults are STEERABLE.

`claude_mcp_servers/scripts/migrate_rl_jsonl_to_db.py` built its two default
paths as MODULE-LEVEL CONSTANTS::

    _DEFAULT_PRIMARY = Path.home() / ".claude" / "retrieval_rl_data" / "rl_events.jsonl"
    _DEFAULT_QWEN3   = Path.home() / ".claude" / "retrieval_rl_data" / "rl_events_qwen3.jsonl"

which is the same unsteerable shape as register item 28 at the WRITE end: the
value freezes at import, so no redirect established afterwards — which is every
redirect a pytest fixture sets — can move it. A guard written against the frozen
value would pass while the real home was being read.

This one legitimately wants the OLD location: the corpus there is FROZEN
(v0.2.47 replaced the JSONL sink with the hub's `rl_events` table) and its four
remaining consumers all address it by name. So the fix is not "point it at the
new home" — it is "resolve it through
`vco_lib.paths.legacy_claude_rl_data_dir()`", which honours `$VCT_CLAUDE_DIR`,
and make the defaults FUNCTIONS so the resolution happens per call.

It also wires `$RL_DATA_DIR` (ruling R24): this module's own docstring had
advertised that override since v0.2.46 and NOTHING in the tree read it. R24's
default for a declared-but-unread knob is to build the reader, not delete the
promise — and to prove that setting it changes something observable.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO_ROOT / "claude_mcp_servers" / "scripts" / "migrate_rl_jsonl_to_db.py"


@pytest.fixture(scope="module")
def migrate_mod():
    """Import the script by path, once.

    Imported ONCE per module deliberately: the whole point of the fix is that
    the resolution is not frozen at import, so every test below runs against
    the SAME already-imported module object with a DIFFERENT environment. A
    per-test re-import would hide exactly the defect this file exists to pin.
    """
    pytest.importorskip("pydantic", reason="rl_client.hub_writer needs pydantic")
    spec = importlib.util.spec_from_file_location(
        "vco_test_migrate_rl_jsonl_to_db", _SCRIPT
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    yield mod
    sys.modules.pop(spec.name, None)


# --------------------------------------------------------------------------- #
# 1. The freeze is gone
# --------------------------------------------------------------------------- #


def test_archive_dir_follows_a_redirect_set_after_import(migrate_mod, tmp_path, monkeypatch):
    """The assertion that fails against the pre-v0.2.92 module-level constants."""
    later = tmp_path / "claude_home_set_after_import"
    monkeypatch.delenv("RL_DATA_DIR", raising=False)
    monkeypatch.setenv("VCT_CLAUDE_DIR", str(later))

    assert migrate_mod.archive_dir() == later / "retrieval_rl_data"
    assert migrate_mod.default_primary() == later / "retrieval_rl_data" / "rl_events.jsonl"
    assert migrate_mod.default_qwen3() == later / "retrieval_rl_data" / "rl_events_qwen3.jsonl"


def test_a_second_redirect_moves_it_again(migrate_mod, tmp_path, monkeypatch):
    """Resolution is per-CALL, not memoised on first use.

    A cache would reproduce the bug one layer down: the first caller in a
    long-lived process would pin the answer for every later one.
    """
    monkeypatch.delenv("RL_DATA_DIR", raising=False)
    monkeypatch.setenv("VCT_CLAUDE_DIR", str(tmp_path / "first"))
    first = migrate_mod.archive_dir()
    monkeypatch.setenv("VCT_CLAUDE_DIR", str(tmp_path / "second"))
    second = migrate_mod.archive_dir()

    assert first != second
    assert second == tmp_path / "second" / "retrieval_rl_data"


def test_module_declares_no_home_derived_path_constant():
    """Static: no ``Path.home()`` CALL anywhere in this file, and no old constants.

    The runtime tests above would pass against a lazily-resolving helper that
    ALSO left the old constants in place for a straggler caller. This asserts
    the shape is gone, not just shadowed.

    Parsed with :mod:`ast` rather than grepped, because the module docstring
    now DISCUSSES ``Path.home()`` at length — deliberately, since recording
    why the constants were wrong is the point of the fix. A text scan would
    flag the explanation and force it to be deleted, which is the failure mode
    where a guard eats its own documentation.
    """
    import ast

    tree = ast.parse(_SCRIPT.read_text(encoding="utf-8"), filename=str(_SCRIPT))

    home_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "home"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "Path"
    ]
    assert not home_calls, (
        f"Path.home() called at line(s) {[n.lineno for n in home_calls]} — an "
        f"inline home is a path no redirect can steer; resolve through "
        f"vco_lib.paths.claude_user_dir() / vct_root_dir()"
    )

    module_level_names = {
        t.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        for t in node.targets
        if isinstance(t, ast.Name)
    }
    assert "_DEFAULT_PRIMARY" not in module_level_names
    assert "_DEFAULT_QWEN3" not in module_level_names


# --------------------------------------------------------------------------- #
# 2. R24: $RL_DATA_DIR now HAS a reader, and it changes what is read
# --------------------------------------------------------------------------- #


def test_rl_data_dir_env_overrides_the_archive(migrate_mod, tmp_path, monkeypatch):
    elsewhere = tmp_path / "mnt" / "backup" / "retrieval_rl_data"
    monkeypatch.setenv("VCT_CLAUDE_DIR", str(tmp_path / "ignored_claude_home"))
    monkeypatch.setenv("RL_DATA_DIR", str(elsewhere))

    assert migrate_mod.archive_dir() == elsewhere
    assert migrate_mod.default_primary() == elsewhere / "rl_events.jsonl"


def test_empty_rl_data_dir_is_treated_as_unset(migrate_mod, tmp_path, monkeypatch):
    """An exported-but-empty env var must not resolve to the process CWD.

    ``RL_DATA_DIR=`` in a shell rc is a common shape and ``Path("")`` is
    ``Path(".")`` — a silent "migrate whatever JSONL happens to be here".
    """
    monkeypatch.setenv("VCT_CLAUDE_DIR", str(tmp_path / "claude_home"))
    monkeypatch.setenv("RL_DATA_DIR", "   ")

    assert migrate_mod.archive_dir() == tmp_path / "claude_home" / "retrieval_rl_data"


def test_the_env_name_is_still_documented(migrate_mod):
    """R16: the knob is named in the docstring AND read by the code.

    Both halves, because either alone is a defect — the promise without the
    reader is what this item fixed, and a reader nobody can discover is the
    mirror image.
    """
    assert migrate_mod.RL_DATA_DIR_ENV == "RL_DATA_DIR"
    assert "RL_DATA_DIR" in (migrate_mod.__doc__ or "")


# --------------------------------------------------------------------------- #
# 3. It reaches the real code path (not just the helper)
# --------------------------------------------------------------------------- #


def test_main_reports_the_resolved_paths_when_nothing_is_there(
    migrate_mod, tmp_path, monkeypatch, capsys
):
    """`main([])` with an empty corpus dir: exit 0, and it NAMES what it checked.

    Proves the resolver is wired into the actual entry point rather than only
    into a helper a test can reach. It returns before the hub probe, so this
    starts no process and needs no launcher.
    """
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    monkeypatch.setenv("RL_DATA_DIR", str(corpus))

    assert migrate_mod.main([]) == 0
    out = capsys.readouterr().out
    assert str(corpus / "rl_events.jsonl") in out
    assert str(corpus / "rl_events_qwen3.jsonl") in out


def test_main_stops_cleanly_when_the_hub_is_down(migrate_mod, tmp_path, monkeypatch, capsys):
    """Corpus present, hub absent ⇒ exit 2 and the file is NOT touched."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    seeded = corpus / "rl_events.jsonl"
    seeded.write_text('{"event": "retrieval", "task_id": "t1"}\n', encoding="utf-8")
    monkeypatch.setenv("RL_DATA_DIR", str(corpus))
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path / "no_hub_here"))

    rc = migrate_mod.main([])

    assert rc == 2
    assert "hub token not found" in capsys.readouterr().err.lower()
    assert seeded.is_file(), "the source file must not be touched on the hub-down path"


def test_main_processes_the_file_the_override_names(
    migrate_mod, tmp_path, monkeypatch, capsys
):
    """The positive half: the file MAIN opens is the one the override names.

    The hub-down variant above cannot prove this on its own, and finding that
    out is worth recording: run against the PRE-v0.2.92 module it still exited
    2 — because ``Path.home()`` had found the maintainer's REAL 2.6 GB corpus
    and the hub probe then stopped it. A test whose green comes from reaching
    the real home is worse than no test.

    So the hub probe is stubbed past (no network, no launcher) and the run is
    ``--dry-run``, which validates and counts but never POSTs and never
    renames. The assertion is on the path the run NAMES, plus an explicit "the
    real home is not in this output".
    """
    hub_writer = pytest.importorskip("rl_client.hub_writer")
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    seeded = corpus / "rl_events.jsonl"
    seeded.write_text('{"event": "retrieval", "task_id": "t1"}\n', encoding="utf-8")
    monkeypatch.setenv("RL_DATA_DIR", str(corpus))
    monkeypatch.setattr(hub_writer, "_read_hub_token", lambda: "stub-token")
    monkeypatch.setattr(hub_writer, "_read_hub_port", lambda: 7700)

    rc = migrate_mod.main(["--dry-run"])
    out = capsys.readouterr().out

    assert rc == 0
    assert str(seeded) in out, out
    assert str(Path.home()) not in out, (
        "the run named a path under the real home — the resolution escaped "
        "the override, which is register item 28's shape at the READ end"
    )
    assert seeded.is_file(), "--dry-run must not rename"


# --------------------------------------------------------------------------- #
# 4. The archive resolver itself, and the vendoring-forced mirror
# --------------------------------------------------------------------------- #


def test_legacy_claude_rl_data_dir_sits_under_claude_user_dir(tmp_path, monkeypatch):
    from vco_lib.paths import claude_user_dir, legacy_claude_rl_data_dir

    monkeypatch.setenv("VCT_CLAUDE_DIR", str(tmp_path / "claude_home"))
    resolved = legacy_claude_rl_data_dir()

    assert resolved.parent == claude_user_dir()
    assert resolved == tmp_path / "claude_home" / "retrieval_rl_data"


def test_legacy_claude_rl_data_dir_never_creates_anything(tmp_path, monkeypatch):
    """Resolving the ARCHIVE must not materialise it.

    Creating it would be a write under ``~/.claude`` — the exact class this
    family exists to stop — and on a post-v0.2.92 machine the archive legitimately
    does not exist at all.
    """
    from vco_lib.paths import legacy_claude_rl_data_dir

    home = tmp_path / "pristine"
    monkeypatch.setenv("VCT_CLAUDE_DIR", str(home))

    resolved = legacy_claude_rl_data_dir()

    assert not resolved.exists()
    assert not home.exists()


def test_the_corpus_dirname_matches_the_loggers_copy():
    """Class-C mirror pin: the archive resolver and the live logger agree.

    ``rl_client/rl_logger.py`` is byte-identical-VENDORED into the paid RL
    container image, which builds WITHOUT ``vco_lib``, so it cannot import the
    shared constant — tier A of the CLAUDE.md A>B>C rule is closed by a
    packaging constraint. The permitted fallback is the mirror plus a parity
    test; this is it.
    """
    pytest.importorskip("pydantic", reason="rl_client package import")
    sys.path.insert(0, str(_REPO_ROOT / "claude_mcp_servers"))
    try:
        from rl_client import rl_logger
    finally:
        sys.path.pop(0)
    from vco_lib.paths import legacy_claude_rl_data_dir

    assert legacy_claude_rl_data_dir().name == rl_logger._RL_DATA_DIRNAME


def test_the_live_home_and_the_archive_are_different_roots(tmp_path, monkeypatch):
    """The move actually happened: logger writes to ~/.vct, importer reads ~/.claude."""
    pytest.importorskip("pydantic", reason="rl_client package import")
    sys.path.insert(0, str(_REPO_ROOT / "claude_mcp_servers"))
    try:
        from rl_client import rl_logger
    finally:
        sys.path.pop(0)
    from vco_lib.paths import legacy_claude_rl_data_dir

    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("VCT_CLAUDE_DIR", str(tmp_path / "claude_home"))

    assert rl_logger.default_rl_data_dir() == tmp_path / "state" / "retrieval_rl_data"
    assert legacy_claude_rl_data_dir() == tmp_path / "claude_home" / "retrieval_rl_data"
