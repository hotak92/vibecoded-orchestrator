# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 W6-LOWS (R3 C-4) — a file-level ``#![cfg(test)]`` counts only
before the file's first item.

``cfg_test_spans`` returned a whole-file span for an inner ``#![cfg(test)]``
ANYWHERE in the file, including the inner attribute of an inline
``mod x { #![cfg(test)] ... }``: every production line was then blanked and
every source-scanning gate built on it went blind. The Rust twin
(``is_whole_file_test_cfg`` in launcher/src-tauri/tests/support/mod.rs) had
the same defect and carries the same rule, pinned by
``gated_items_end_at_their_own_terminator`` in command_silent_gate.rs.
"""
from __future__ import annotations

import pytest

from tests.common.rust_source import cfg_test_spans, production_source

INLINE_MOD = (
    "fn production() {\n"
    "    let _ = std::process::Command::new(\"git\").status();\n"
    "}\n"
    "mod x {\n"
    "    #![cfg(test)]\n"
    "    fn t() {}\n"
    "}\n"
    "fn after() {}\n"
)


def test_inline_module_inner_cfg_test_is_not_whole_file():
    spans = cfg_test_spans(INLINE_MOD)
    assert (1, 8) not in spans, spans
    prod = production_source(INLINE_MOD)
    assert "Command::new" in prod and "fn after()" in prod


def test_inline_module_inner_cfg_test_any_test_reading():
    assert (1, 8) not in cfg_test_spans(INLINE_MOD, include_any_test=True)


@pytest.mark.parametrize("src", [
    pytest.param("#![cfg(test)]\nfn f() {}\n", id="first-line"),
    pytest.param("// docs\n\n//! crate docs\n#![cfg(test)]\nfn f() {}\n", id="after-comments"),
    pytest.param("#![allow(dead_code)]\n#![cfg(test)]\nfn f() {}\n", id="after-inner-attr"),
    pytest.param("#![allow(\n    dead_code,\n)]\n  #! [ cfg ( test ) ]\nfn f() {}\n", id="after-multiline-inner-attr"),
    pytest.param("#![allow(dead_code)] #![cfg(test)]\nfn f() {}\n", id="two-on-one-line"),
    pytest.param("/* a\n b */\n#![cfg(all(test, unix))]\nfn f() {}\n", id="all-test-predicate"),
    pytest.param("#!/usr/bin/env run-cargo-script\n#![cfg(test)]\nfn f() {}\n", id="after-shebang"),
])
def test_leading_inner_cfg_test_is_whole_file(src):
    n = len(src.splitlines())
    assert cfg_test_spans(src) == [(1, n)]


@pytest.mark.parametrize("src", [
    pytest.param("use std::fmt;\n#![cfg(test)]\nfn f() {}\n", id="after-use"),
    pytest.param("fn f() {}\n#![cfg(test)]\n", id="after-fn"),
    pytest.param("\n#!/bin/x\n#![cfg(test)]\nfn f() {}\n", id="bang-not-on-first-line"),
    pytest.param("#![allow(dead_code)]\nconst A: u8 = 1;\n#![cfg(test)]\n", id="after-const"),
    pytest.param("#![cfg(unix)]\nfn f() {}\n", id="not-test"),
])
def test_inner_cfg_test_after_an_item_or_not_test_is_not_whole_file(src):
    n = len(src.splitlines())
    assert (1, n) not in cfg_test_spans(src)
