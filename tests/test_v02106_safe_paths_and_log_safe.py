# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.106 CodeQL hardening: ``vco_lib.safe_paths`` (request-supplied paths
contained before a file operation), ``vco_lib.log_setup.log_safe`` (one log
line per record) and the shared project-id rule
(``rl_client.rerank_wire.sanitize_project_id``)."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from rl_client.rerank_wire import sanitize_project_id
from vco_lib.log_setup import log_safe
from vco_lib.safe_paths import contained_path, existing_entry, locate_existing


@pytest.fixture
def tree(tmp_path: Path) -> dict[str, Path]:
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "w.pt").write_bytes(b"x")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.pt").write_bytes(b"s")
    (tmp_path / "root-x").mkdir()
    (tmp_path / "root-x" / "w.pt").write_bytes(b"y")
    return {"tmp": tmp_path, "root": root, "outside": outside}


def test_contained_path_accepts_inside_and_returns_the_resolved_path(tree):
    root = tree["root"]
    assert contained_path(root / "sub" / "w.pt", (root,)) == str(root / "sub" / "w.pt")
    spelled = root / "sub" / ".." / "sub" / "." / "w.pt"
    assert contained_path(spelled, (root,)) == str(root / "sub" / "w.pt")
    # A path that does not exist yet (a dir to create) is fine too.
    assert contained_path(root / "new" / "x", (root,)) == str(root / "new" / "x")


@pytest.mark.parametrize("bad", [
    "{root}/../outside/secret.pt",   # traversal
    "{root}-x/w.pt",                  # sibling sharing the prefix string
    "{root}",                         # the root itself is not inside it
    "/etc/passwd",
])
def test_contained_path_refuses_outside(tree, bad):
    root = tree["root"]
    assert contained_path(bad.format(root=root), (root,)) is None


def test_contained_path_refuses_a_symlink_out_of_the_root(tree):
    root = tree["root"]
    link = root / "link.pt"
    link.symlink_to(tree["outside"] / "secret.pt")
    dir_link = root / "dirlink"
    dir_link.symlink_to(tree["outside"])
    assert contained_path(link, (root,)) is None
    assert contained_path(dir_link / "secret.pt", (root,)) is None
    assert locate_existing(link, (root,)) is None


def test_contained_path_never_raises_on_junk():
    assert contained_path("bad\x00path", ("/tmp",)) is None


def test_existing_entry_matches_names_exactly(tree):
    root = tree["root"]
    assert existing_entry(root, "sub") == os.path.join(str(root), "sub")
    for name in ("..", ".", "sub/w.pt", "SUB", "", "nope"):
        assert existing_entry(root, name) is None
    assert existing_entry(tree["tmp"] / "missing-dir", "x") is None


def test_locate_existing_rebuilds_the_path_from_the_listing(tree):
    root = tree["root"]
    spelled = root / "sub" / ".." / "sub" / "w.pt"
    assert locate_existing(spelled, (tree["outside"], root)) == str(root / "sub" / "w.pt")
    assert locate_existing(root / "sub" / "missing.pt", (root,)) is None
    assert locate_existing(tree["outside"] / "secret.pt", (root,)) is None


def test_log_safe_keeps_one_record_per_line():
    """RED-PROOF (CodeQL py/log-injection): CR / LF / control characters in a
    logged value are spelled out, never written raw."""
    forged = "proj\nINFO routing: loaded_project_head project_id=admin\r\x1b[2K"
    out = log_safe(forged)
    assert "\n" not in out and "\r" not in out and "\x1b" not in out
    assert out == "proj\\nINFO routing: loaded_project_head project_id=admin\\r\\x1b[2K"
    assert log_safe("a\r\nb") == "a\\r\\nb"
    assert log_safe(Path("/data/state/x.pt")) == "/data/state/x.pt"
    assert log_safe("plain-id_1") == "plain-id_1"


def test_model_router_uses_the_one_log_safe():
    from model_router import server as gw

    assert gw._log_safe is log_safe


@pytest.mark.parametrize("value", ["abc\n", "abc\r\n", "../etc", "a b", "", "x" * 65, None])
def test_sanitize_project_id_refuses(value):
    """RED-PROOF: ``^...$`` with ``match`` accepted ``"abc\\n"`` (``$`` matches
    before a trailing newline); the rule is now a ``fullmatch``."""
    assert sanitize_project_id(value) is None


def test_sanitize_project_id_is_the_clients_rule():
    from rl_client.client import sanitize_project_id as client_rule

    assert client_rule is sanitize_project_id
    assert sanitize_project_id("02fbc934-ada5-433c-b606-d1f56194035a")
    assert sanitize_project_id("x" * 64) == "x" * 64
