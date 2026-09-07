# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 WP-5b — two declarations of one type name must be two rows.

WHY THIS EXISTS
---------------
Seven extractors built their type table as ``Dict[str, int]`` keyed by the
type NAME, so a second declaration with the same name silently OVERWROTE the
first: one row survived, with the last declaration's line range and body, and
the earlier declaration's body was unreachable in the graph. It is the same
loss class v0.2.92 fixed for FUNCTIONS (``engine.rs``'s two ``fn reset``) via
``assign_duplicate_identity_suffixes``, which groups on ``(kind,
identity_key)`` and has therefore always covered ``KIND_CLASS`` — the writer
was ready; the producers threw the second declaration away before it got there.

WHY ALL SIX (not only the one whose fixture proves it). Ruby class REOPENING is
idiomatic and the golden corpus exhibits it, which is why ``ledger.rb`` is where
the diff shows. But the loss is REACHABLE FROM LEGAL SOURCE in all of them, and
"pre-existing" is not a disposition:

    ruby        `class Account` twice — reopening, the idiomatic form
    csharp      two namespaces in one file; a `partial class` split in-file
    java        `class A { class Node {} } class B { class Node {} }`
    cpp         `namespace a { struct Cfg {…}; } namespace b { struct Cfg {…}; }`
    go          a local `type cfg struct` in two different function bodies
    rust        `struct Cfg` at module level and again inside `mod tests`
    javascript  a block-scoped `class Cfg` in two branches / two factories

None of these extractors qualifies ``full_name`` by namespace / package / mod /
block, so all of them collide.

WHAT IS PINNED
--------------
* the ACT: two declarations -> two entities, with DIFFERENT bodies and their
  own line ranges (two rows carrying identical text would be the clobber
  wearing a disguise);
* the LEAVE-ALONE, per language: ordinary input with no duplicate name yields
  exactly one row per type, in source order, and the module summary lists each
  name ONCE — the conversion from ``dict.keys()`` to
  ``dict.fromkeys(...)`` is only byte-identical if that holds.
"""
from __future__ import annotations

from typing import Any, List

import pytest

from vco_lib.codegraph_entities import KIND_CLASS
from vco_lib.codegraph_lang.cpp import extract_cpp_file
from vco_lib.codegraph_lang.csharp import extract_csharp_file
from vco_lib.codegraph_lang.go import extract_go_file
from vco_lib.codegraph_lang.java import extract_java_file
from vco_lib.codegraph_lang.javascript import extract_js_file
from vco_lib.codegraph_lang.ruby import extract_ruby_file
from vco_lib.codegraph_lang.rust import extract_rust_file


class _Helpers:
    project_name = "DupProj"

    def embed_class(self, *_a: Any, **_k: Any) -> None:
        return None

    def embed_function(self, *_a: Any, **_k: Any) -> None:
        return None

    def generate_embedding(self, *_a: Any, **_k: Any) -> None:
        return None


def _classes(extractor, name: str, source: str, tmp_path) -> List[Any]:
    target = tmp_path / name
    target.write_text(source, encoding="utf-8")
    fx = extractor(source, target, tmp_path, _Helpers())
    return [e for e in fx.entities if e.kind == KIND_CLASS]


def _summary_names(extractor, name: str, source: str, tmp_path, label: str) -> List[str]:
    target = tmp_path / name
    target.write_text(source, encoding="utf-8")
    fx = extractor(source, target, tmp_path, _Helpers())
    line = next(
        (ln for ln in fx.module.module_summary.split("\n") if ln.startswith(label)), ""
    )
    return [n.strip() for n in line[len(label):].split(",") if n.strip()]


# ── the DUPLICATE case, per language ───────────────────────────────────────
_DUPLICATES = [
    pytest.param(
        extract_ruby_file, "ledger.rb",
        "class Cfg\n  def a\n  end\nend\n\nclass Cfg\n  def b\n  end\nend\n",
        "Cfg", "ledger.Cfg", id="ruby-reopening",
    ),
    pytest.param(
        extract_csharp_file, "Dup.cs",
        "namespace A\n{\n    public class Cfg\n    {\n        public int A() { return 1; }\n    }\n}\n"
        "namespace B\n{\n    public class Cfg\n    {\n        public int B() { return 2; }\n    }\n}\n",
        "Cfg", "A.Cfg", id="csharp-two-namespaces",
    ),
    pytest.param(
        extract_java_file, "Dup.java",
        "class A {\n    class Node {\n        int a() { return 1; }\n    }\n}\n"
        "class B {\n    class Node {\n        int b() { return 2; }\n    }\n}\n",
        "Node", "Node", id="java-two-nested",
    ),
    pytest.param(
        extract_cpp_file, "dup.cpp",
        "namespace a {\nstruct Cfg {\n    int x;\n};\n}\n"
        "namespace b {\nstruct Cfg {\n    double y;\n};\n}\n",
        "Cfg", "dup.Cfg", id="cpp-two-namespaces",
    ),
    pytest.param(
        extract_go_file, "dup.go",
        "package main\n\nfunc f() {\n\ttype cfg struct {\n\t\tA int\n\t}\n}\n\n"
        "func g() {\n\ttype cfg struct {\n\t\tB string\n\t}\n}\n",
        "cfg", "main.cfg", id="go-two-function-locals",
    ),
    pytest.param(
        extract_rust_file, "dup.rs",
        "pub struct Cfg {\n    a: u8,\n}\n\nmod tests {\n    pub struct Cfg {\n        b: u16,\n    }\n}\n",
        "Cfg", "dup.Cfg", id="rust-module-and-mod-block",
    ),
    pytest.param(
        extract_js_file, "dup.js",
        "function makeA() {\n  class Cfg {\n    a() { return 1; }\n  }\n  return Cfg;\n}\n"
        "function makeB() {\n  class Cfg {\n    b() { return 2; }\n  }\n  return Cfg;\n}\n",
        "Cfg", "dup.Cfg", id="javascript-two-block-scopes",
    ),
]


@pytest.mark.parametrize("extractor,fname,source,name,full_name", _DUPLICATES)
def test_two_declarations_of_one_name_are_two_rows(
    extractor, fname, source, name, full_name, tmp_path
) -> None:
    rows = [c for c in _classes(extractor, fname, source, tmp_path) if c.name == name]
    assert len(rows) == 2, (
        f"{fname}: {len(rows)} row(s) for two declarations of {name!r} — "
        "the second overwrote the first"
    )
    assert all(c.full_name == full_name for c in rows)


@pytest.mark.parametrize("extractor,fname,source,name,full_name", _DUPLICATES)
def test_the_two_rows_carry_different_text(
    extractor, fname, source, name, full_name, tmp_path
) -> None:
    """Two rows with identical bodies would be the clobber in disguise — the
    exact failure the ``fn reset`` fix had to distinguish from a real split."""
    rows = [c for c in _classes(extractor, fname, source, tmp_path) if c.name == name]
    assert len({(c.body or "") for c in rows}) == 2
    assert len({c.start_line for c in rows}) == 2


# ── the LEAVE-ALONE case, per language ─────────────────────────────────────
_SINGLES = [
    pytest.param(
        extract_ruby_file, "s.rb",
        "class A\n  def a\n  end\nend\n\nclass B\n  def b\n  end\nend\n",
        ["A", "B"], "Classes: ", id="ruby",
    ),
    pytest.param(
        extract_csharp_file, "S.cs",
        "namespace N\n{\n    public class A\n    {\n        public int F() { return 1; }\n    }\n"
        "    public class B\n    {\n        public int G() { return 2; }\n    }\n}\n",
        ["A", "B"], "Classes: ", id="csharp",
    ),
    pytest.param(
        extract_java_file, "S.java",
        "class A {\n    int a() { return 1; }\n}\nclass B {\n    int b() { return 2; }\n}\n",
        ["A", "B"], "Classes: ", id="java",
    ),
    pytest.param(
        extract_cpp_file, "s.cpp",
        "struct A {\n    int x;\n};\nstruct B {\n    int y;\n};\n",
        ["A", "B"], "Classes: ", id="cpp",
    ),
    pytest.param(
        extract_go_file, "s.go",
        "package main\n\ntype A struct {\n\tX int\n}\n\ntype B struct {\n\tY int\n}\n",
        ["A", "B"], "Types: ", id="go",
    ),
    pytest.param(
        extract_rust_file, "s.rs",
        "pub struct A {\n    x: u8,\n}\n\npub struct B {\n    y: u8,\n}\n",
        ["A", "B"], "Types: ", id="rust",
    ),
    pytest.param(
        extract_js_file, "s.js",
        "class A {\n  a() { return 1; }\n}\nclass B {\n  b() { return 2; }\n}\n",
        ["A", "B"], "Classes: ", id="javascript",
    ),
]


@pytest.mark.parametrize("extractor,fname,source,names,label", _SINGLES)
def test_ordinary_input_yields_one_row_per_type_in_source_order(
    extractor, fname, source, names, label, tmp_path
) -> None:
    rows = _classes(extractor, fname, source, tmp_path)
    assert [c.name for c in rows] == names


@pytest.mark.parametrize("extractor,fname,source,names,label", _SINGLES)
def test_the_module_summary_still_lists_each_name_once(
    extractor, fname, source, names, label, tmp_path
) -> None:
    """The conversion replaced ``dict.keys()`` with ``dict.fromkeys(...)``.
    Byte-identical output depends on that producing the same list, which is
    what makes the golden diff for the other six languages EMPTY."""
    assert _summary_names(extractor, fname, source, tmp_path, label) == names


@pytest.mark.parametrize("extractor,fname,source,name,full_name", _DUPLICATES)
def test_a_reopened_name_is_listed_once_in_the_module_summary(
    extractor, fname, source, name, full_name, tmp_path
) -> None:
    """Two ROWS, one NAME: the summary is a list of the file's types, and a
    type declared twice is still one type.

    ``javascript.py`` is exempt: its summary list has always been built by
    appending per match, and no defect in this package demonstrates a cost to
    that, so leaving it alone is the conservative call rather than a silent
    behaviour change ridden in on an unrelated fix.
    """
    if fname.endswith(".js"):
        pytest.skip("javascript's summary list is append-per-match by design")
    label = "Types: " if fname.endswith((".go", ".rs")) else "Classes: "
    listed = _summary_names(extractor, fname, source, tmp_path, label)
    assert listed.count(name) == 1, listed
