# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""tree-sitter DEFINITIONS layer for the code graph (v0.2.102 W6-E).

What this module is
-------------------
The per-language extractors in :mod:`vco_lib.codegraph_lang` find a file's
classes, functions and methods. Until v0.2.102 every non-Python extractor did
that with regular expressions plus brace / ``end``-keyword scanning, which
mis-extracts whole construct families (measured: TypeScript generic functions
and interfaces invisible, a return-type object literal truncating a body, a
Rust ``fn`` minted from a string literal, ``#[tokio::test]`` functions indexed
as production code).

:func:`extract_definitions` parses the file with the language's tree-sitter
grammar (loaded through the ONE grammar home,
:mod:`vco_lib.codegraph_grammars`) and returns a language-neutral
:class:`Definitions` — names, exact 1-indexed line ranges, container (class /
impl / module) membership, parameter text, async-ness. Each extractor maps
those onto the SAME ``CodeEntity`` records it has always produced (same
``full_name`` scheme, same signature strings, same ``methods`` extras), so the
analyzer's writer and the Weaviate collections are unchanged; only what is
found, and where it starts and ends, became exact.

Fallback
--------
:func:`extract_definitions` returns ``None`` when the grammar cannot be loaded
(the ``codegraph-ts`` extra is absent) or the language has no grammar. The
extractor then runs its regex discovery exactly as before. Which path ran is
logged ONCE per process per language (:func:`note_path`).

Policies (shared by every language — the ground-truth fixtures under
``tests/fixtures/codegraph_truth/`` are written to these):

* ``start_line`` is the first line of the declaration proper — export /
  visibility / modifier keywords and C++ ``template<…>`` heads included;
  decorators, annotations and attributes EXCLUDED (they are separate syntax
  in Rust and were never part of a regex match elsewhere).
* ``end_line`` is the declaration's closing line (``}``, ``end``, ``;``).
* Nested declarations are found at any depth (as the regex extractors did);
  their ``full_name`` stays whatever the extractor already used.
* Rust: functions and types under a test-function attribute (``#[test]``,
  ``#[tokio::test]``, ``#[rstest]`` — any path ending in ``test`` /
  ``rstest``) or a TEST-ONLY ``cfg`` on themselves or on any enclosing
  ``mod`` / ``impl`` are test code and are not returned. Test-only follows the
  repo's own rule (``launcher/src-tauri/tests/command_silent_gate.rs``
  ``is_test_cfg_attr``): ``cfg(test)``, ``cfg(all(test, …))``,
  ``cfg(any(test, debug_assertions))``; NOT ``cfg(any(target_os = "linux",
  test))``, ``cfg(any(test, feature = "x"))`` or ``cfg(not(test))``.
  Limitation: ``#[cfg(test)] mod tests;`` gates a DIFFERENT file, which is
  analyzed on its own and is therefore not recognised as test code.
* A file whose parse tree has an error takes the regex path whole (see
  :func:`_has_parse_error`).
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import AbstractSet, Callable, Dict, Iterable, List, Optional, Set, Tuple

from vco_lib.codegraph_grammars import parse

logger = logging.getLogger(__name__)

KIND_CLASS = "class"
KIND_FUNCTION = "function"


@dataclass
class Definition:
    """One extracted definition, language-neutral."""

    kind: str                       # KIND_CLASS | KIND_FUNCTION
    name: str
    start_line: int                 # 1-indexed
    end_line: int                   # 1-indexed, closing line
    keyword: str = ""               # class / interface / struct / enum / trait / module / record / function …
    params: str = ""                # parameter list text WITHOUT the outer delimiters
    is_async: bool = False
    container: Optional[str] = None  # innermost enclosing class-like name (methods)
    qualifier: Optional[str] = None  # explicit owner written in the name (C++ ``A::f``, Lua ``T.f``)
    base: Optional[str] = None      # first superclass / extends target
    has_body: bool = True
    exported: bool = False
    tag: str = ""                   # per-language sub-kind (e.g. svelte ``reactive`` / ``arrow_export``)
    attributes: List[str] = field(default_factory=list)  # raw attribute/annotation texts (C# routes)
    start_col: int = 0              # 0-indexed CHARACTER column of ``start_line``


@dataclass
class Definitions:
    """All definitions of one file, each list in source order."""

    classes: List[Definition] = field(default_factory=list)
    functions: List[Definition] = field(default_factory=list)
    #: class name -> member method names (source order, de-duplicated), the
    #: union over every same-named declaration (Ruby reopening, Rust impls).
    methods: Dict[str, List[str]] = field(default_factory=dict)

    def add_method(self, owner: str, name: str) -> None:
        names = self.methods.setdefault(owner, [])
        if name not in names:
            names.append(name)

    def methods_of(self, owner: str) -> List[str]:
        return list(self.methods.get(owner, []))


# ---------------------------------------------------------------------------
# Path logging — once per process per language.
# ---------------------------------------------------------------------------

_PATH_LOGGED: Set[Tuple[str, str]] = set()
_PATH_LOCK = threading.Lock()


def note_path(language: str, path: str) -> None:
    """Log once per process which definitions path ran for ``language``
    (``"tree-sitter"`` or ``"regex"``)."""
    key = (language, path)
    if key in _PATH_LOGGED:
        return
    with _PATH_LOCK:
        if key in _PATH_LOGGED:
            return
        _PATH_LOGGED.add(key)
    logger.info("codegraph_defs: %s definitions via %s", language, path)


# ---------------------------------------------------------------------------
# Node helpers
# ---------------------------------------------------------------------------


class _Src:
    """The parsed file's bytes + per-line text, for exact text/column maps."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.lines = data.split(b"\n")

    def text(self, node) -> str:  # noqa: ANN001 — ts node
        return self.data[node.start_byte:node.end_byte].decode("utf-8", "replace")

    def char_col(self, row: int, byte_col: int) -> int:
        if row >= len(self.lines):
            return byte_col
        return len(self.lines[row][:byte_col].decode("utf-8", "replace"))


def _start_line(node) -> int:  # noqa: ANN001
    return node.start_point[0] + 1


def _end_line(node) -> int:  # noqa: ANN001
    row, col = node.end_point
    if col == 0 and row > node.start_point[0]:
        row -= 1  # node ends with its newline — the closing line is the one before
    return row + 1


def _field_text(src: _Src, node, name: str) -> str:  # noqa: ANN001
    child = node.child_by_field_name(name)
    return src.text(child) if child is not None else ""


def _strip_delims(text: str, open_ch: str = "(", close_ch: str = ")") -> str:
    text = text.strip()
    if text.startswith(open_ch) and text.endswith(close_ch):
        return text[1:-1]
    return text


def _decl_start(node, skip: AbstractSet[str], descend: AbstractSet[str] = frozenset()):  # noqa: ANN001,ANN202
    """The first token of ``node`` that is not a decorator/annotation/attribute.

    Children whose type is in ``skip`` (and comments) are passed over;
    children whose type is in ``descend`` (e.g. Java ``modifiers``, which
    mixes annotations with keywords) are searched inside. Returns the node
    whose start is the declaration's start (``node`` itself as fallback).
    """
    for child in node.children:
        if child.type in skip or child.type == "comment":
            continue
        if child.type in descend:
            inner = _decl_start(child, skip)
            if inner is not child:
                return inner
            continue
        return child
    return node


def _has_child_token(node, token: str) -> bool:  # noqa: ANN001
    return any(c.type == token for c in node.children)


def _walk(node, visit: Callable, ancestors: Tuple = ()) -> None:  # noqa: ANN001
    """Pre-order walk; ``visit(node, ancestors)`` returns False to prune."""
    stack = [(node, ancestors)]
    while stack:
        n, anc = stack.pop()
        if visit(n, anc) is False:
            continue
        child_anc = anc + (n,)
        for child in reversed(n.children):
            if child.is_named:
                stack.append((child, child_anc))


def _make(src: _Src, kind: str, name: str, start_node, end_node, **kw) -> Definition:  # noqa: ANN001
    row, bcol = start_node.start_point
    return Definition(
        kind=kind, name=name,
        start_line=row + 1, end_line=_end_line(end_node),
        start_col=src.char_col(row, bcol), **kw,
    )


# ---------------------------------------------------------------------------
# TypeScript / JavaScript (and Svelte <script> blocks)
# ---------------------------------------------------------------------------

_JS_CLASS_TYPES = {
    "class_declaration": "class",
    "abstract_class_declaration": "class",
    "interface_declaration": "interface",
    # TypeScript type-level declarations (v0.2.102 review LOW-4) — named,
    # searchable units with no methods of their own.
    "enum_declaration": "enum",
    "type_alias_declaration": "type",
    "internal_module": "namespace",
    "module": "namespace",
}
_JS_FUNC_TYPES = {"function_declaration", "generator_function_declaration"}
_JS_FUNC_VALUE_TYPES = {"arrow_function", "function_expression", "function", "generator_function"}
_JS_METHOD_TYPES = {"method_definition", "method_signature", "abstract_method_signature"}
_JS_SKIP = {"decorator"}


def _js_outer(node):  # noqa: ANN001,ANN202
    """``export`` wrapper when the declaration is exported, else the node."""
    parent = node.parent
    if parent is not None and parent.type == "export_statement":
        return parent
    return node


def _js_heritage_base(src: _Src, node) -> Optional[str]:  # noqa: ANN001
    for child in node.children:
        if child.type == "class_heritage":
            for clause in child.children:
                if clause.type == "extends_clause":
                    value = clause.child_by_field_name("value")
                    if value is None:
                        named = [c for c in clause.named_children]
                        value = named[0] if named else None
                    if value is not None:
                        return src.text(value)
            # JavaScript grammar: class_heritage is `extends <expr>` directly
            named = child.named_children
            if named and named[0].type != "extends_clause" and named[0].type != "implements_clause":
                return src.text(named[0])
        if child.type == "extends_type_clause":  # interface X extends Y
            named = child.named_children
            if named:
                return src.text(named[0])
    return None


def _js_definitions(src: _Src, root, *, svelte_module: Optional[bool] = None) -> Definitions:  # noqa: ANN001
    defs = Definitions()

    def visit(node, anc):  # noqa: ANN001,ANN202
        t = node.type
        if t in _JS_CLASS_TYPES:
            name_node = node.child_by_field_name("name")
            if name_node is None or name_node.type == "string":  # `declare module "x"`
                return True
            outer = _js_outer(node)
            start = _decl_start(outer, _JS_SKIP) if outer is not node else _decl_start(node, _JS_SKIP)
            cname = src.text(name_node)
            defs.classes.append(_make(
                src, KIND_CLASS, cname, start, outer,
                keyword=_JS_CLASS_TYPES[t], base=_js_heritage_base(src, node),
                exported=outer is not node,
            ))
            body = node.child_by_field_name("body")
            if body is not None:
                for member in body.named_children:
                    value = member.child_by_field_name("value") if member.type == "public_field_definition" else None
                    is_field_fn = value is not None and value.type in _JS_FUNC_VALUE_TYPES
                    if member.type in _JS_METHOD_TYPES or is_field_fn:  # `onClick = () => {…}`
                        mname = member.child_by_field_name("name")
                        if mname is not None:
                            defs.add_method(cname, src.text(mname))
            return True
        if t in _JS_FUNC_TYPES:
            name_node = node.child_by_field_name("name")
            if name_node is None:
                return True
            outer = _js_outer(node)
            start = _decl_start(outer, _JS_SKIP)
            defs.functions.append(_make(
                src, KIND_FUNCTION, src.text(name_node), start, node,
                keyword="function", params=_strip_delims(_field_text(src, node, "parameters")),
                is_async=_has_child_token(node, "async"), exported=outer is not node,
                tag="export" if outer is not node else "function",
            ))
            return True
        if t == "function_signature" and node.parent is not None and node.parent.type == "ambient_declaration":
            # `declare function f(): T;` — an ambient declaration is the only
            # definition there is. (Overload signatures beside an
            # implementation are NOT rows; the implementation is.)
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                ambient = node.parent
                outer = _js_outer(ambient)
                defs.functions.append(_make(
                    src, KIND_FUNCTION, src.text(name_node), _decl_start(outer, _JS_SKIP), outer,
                    keyword="function", params=_strip_delims(_field_text(src, node, "parameters")),
                    exported=outer is not ambient, has_body=False,
                    tag="export" if outer is not ambient else "function",
                ))
            return True
        if t in ("lexical_declaration", "variable_declaration"):
            outer = _js_outer(node)
            for decl in node.named_children:
                if decl.type != "variable_declarator":
                    continue
                name_node = decl.child_by_field_name("name")
                value = decl.child_by_field_name("value")
                if name_node is None or value is None or name_node.type != "identifier":
                    continue
                if value.type not in _JS_FUNC_VALUE_TYPES:
                    continue
                defs.functions.append(_make(
                    src, KIND_FUNCTION, src.text(name_node), outer, outer,
                    keyword="const", params=_strip_delims(_field_text(src, value, "parameters")),
                    is_async=_has_child_token(value, "async"), exported=outer is not node,
                    tag="arrow_export" if outer is not node else "arrow",
                ))
            return True
        if svelte_module is False and t == "labeled_statement" and not anc[1:]:
            # Svelte reactive declaration `$: name = …` (default script, top level).
            label = node.child_by_field_name("label")
            body = node.child_by_field_name("body")
            if label is not None and src.text(label) == "$" and body is not None:
                expr = body.named_children[0] if body.type == "expression_statement" and body.named_children else None
                if expr is not None and expr.type == "assignment_expression":
                    left = expr.child_by_field_name("left")
                    if left is not None and left.type == "identifier":
                        defs.functions.append(_make(
                            src, KIND_FUNCTION, src.text(left), node, node,
                            keyword="$:", tag="reactive",
                        ))
            return True
        return True

    _walk(root, visit)
    return defs


# ---------------------------------------------------------------------------
# Rust
# ---------------------------------------------------------------------------

_RUST_TYPE_ITEMS = {"struct_item": "struct", "enum_item": "enum", "trait_item": "trait"}
_RUST_FN_ITEMS = {"function_item", "function_signature_item"}


#: Attribute macros whose LAST path segment marks a test function:
#: ``#[test]``, ``#[tokio::test]``, ``#[async_std::test]``, ``#[rstest]``.
_RUST_TEST_ATTR_LEAVES = frozenset({"test", "rstest"})

#: Bare cfg predicates an ``any(...)`` may combine with ``test`` and still be
#: test-only — the repo's own convention (``launcher/src-tauri/tests/
#: command_silent_gate.rs::is_test_cfg_attr`` treats ``any(test,
#: debug_assertions)`` as test-only and ``any(test, feature = "x")`` as not).
_RUST_TEST_ONLY_ANY_ARGS = frozenset({"test", "debug_assertions"})


def _parse_cfg(text: str):  # noqa: ANN202 — nested list/str tree
    """Parse a cfg predicate into ``str`` leaves and ``(name, [args])`` calls.
    ``feature = "x"`` becomes the leaf ``'feature="x"'``. Unparseable input
    yields ``None`` (callers then treat it as NOT test-only — conservative)."""
    toks: List[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch.isspace():
            i += 1
        elif ch in "(),=":
            toks.append(ch)
            i += 1
        elif ch == '"':
            j = text.find('"', i + 1)
            if j == -1:
                return None
            toks.append(text[i:j + 1])
            i = j + 1
        else:
            j = i
            while j < len(text) and not text[j].isspace() and text[j] not in '(),="':
                j += 1
            toks.append(text[i:j])
            i = j
    pos = 0

    def expr():  # noqa: ANN202
        nonlocal pos
        if pos >= len(toks):
            raise ValueError("eof")
        name = toks[pos]
        pos += 1
        if pos < len(toks) and toks[pos] == "=":
            pos += 1
            value = toks[pos] if pos < len(toks) else ""
            pos += 1
            return f"{name}={value}"
        if pos < len(toks) and toks[pos] == "(":
            pos += 1
            args = []
            while pos < len(toks) and toks[pos] != ")":
                args.append(expr())
                if pos < len(toks) and toks[pos] == ",":
                    pos += 1
            pos += 1  # ')'
            return (name, args)
        return name

    try:
        tree = expr()
    except (ValueError, IndexError):
        return None
    return tree if pos == len(toks) else None


def _cfg_is_test_only(node) -> bool:  # noqa: ANN001
    """True when the cfg predicate holds ONLY in test builds:
    ``test``; ``all(…)`` with any test-only argument; ``any(…)`` whose every
    argument is ``test`` / ``debug_assertions`` (at least one ``test``).
    ``not(…)`` and everything else gate production code."""
    if isinstance(node, str):
        return node == "test"
    if not isinstance(node, tuple):
        return False
    name, args = node
    if name == "all":
        return any(_cfg_is_test_only(a) for a in args)
    if name == "any":
        return (bool(args) and all(isinstance(a, str) and a in _RUST_TEST_ONLY_ANY_ARGS for a in args)
                and "test" in args)
    return False


def _rust_attr_is_test(text: str) -> bool:
    """Is the attribute item ``text`` (``#[…]`` / ``#![…]``) a test gate?
    Test-function macros (:data:`_RUST_TEST_ATTR_LEAVES`) and test-only
    ``cfg(...)`` predicates (:func:`_cfg_is_test_only`)."""
    inner = text.strip().lstrip("#").lstrip("!").strip()
    if inner.startswith("[") and inner.endswith("]"):
        inner = inner[1:-1].strip()
    path = inner.split("(", 1)[0].strip()
    if path.split("::")[-1].strip() in _RUST_TEST_ATTR_LEAVES:
        return True
    if path == "cfg":
        tree = _parse_cfg(inner)
        if isinstance(tree, tuple) and tree[0] == "cfg" and len(tree[1]) == 1:
            return _cfg_is_test_only(tree[1][0])
    return False


def _rust_outer_attrs(src: _Src, node) -> List[str]:  # noqa: ANN001
    """The ``#[…]`` attribute items immediately preceding ``node`` (siblings,
    comments in between tolerated)."""
    attrs: List[str] = []
    prev = node.prev_sibling
    while prev is not None and prev.type in ("attribute_item", "line_comment", "block_comment"):
        if prev.type == "attribute_item":
            attrs.append(src.text(prev))
        prev = prev.prev_sibling
    return attrs


def _rust_inner_attrs(src: _Src, container) -> List[str]:  # noqa: ANN001
    """``#![…]`` inner attributes at the top of a ``mod`` body / the file."""
    body = container.child_by_field_name("body") if container.type == "mod_item" else container
    if body is None:
        return []
    return [src.text(c) for c in body.named_children if c.type == "inner_attribute_item"]


def _rust_is_test_gated(src: _Src, node, anc: Tuple) -> bool:  # noqa: ANN001
    if any(_rust_attr_is_test(a) for a in _rust_outer_attrs(src, node)):
        return True
    for a in anc:
        if a.type in ("mod_item", "impl_item", "trait_item", "function_item"):
            if any(_rust_attr_is_test(x) for x in _rust_outer_attrs(src, a)):
                return True
        if a.type in ("mod_item", "source_file"):
            if any(_rust_attr_is_test(x) for x in _rust_inner_attrs(src, a)):
                return True
    return False


def _rust_type_name(src: _Src, type_node) -> Optional[str]:  # noqa: ANN001
    """Base identifier of an impl target: ``Foo``, ``Foo<T>``, ``a::Foo`` → ``Foo``."""
    if type_node is None:
        return None
    t = type_node.type
    if t == "type_identifier":
        return src.text(type_node)
    if t == "generic_type":
        return _rust_type_name(src, type_node.child_by_field_name("type"))
    if t == "scoped_type_identifier":
        return _rust_type_name(src, type_node.child_by_field_name("name"))
    if t in ("reference_type", "pointer_type"):
        return _rust_type_name(src, type_node.child_by_field_name("type"))
    return None


def _rust_definitions(src: _Src, root) -> Definitions:  # noqa: ANN001
    defs = Definitions()

    def visit(node, anc):  # noqa: ANN001,ANN202
        t = node.type
        if (t in ("mod_item", "impl_item", "function_item", *_RUST_TYPE_ITEMS)
                and _rust_is_test_gated(src, node, anc)):
            return False  # the whole subtree is test code
        if t in _RUST_TYPE_ITEMS:
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                tname = src.text(name_node)
                defs.classes.append(_make(src, KIND_CLASS, tname, node, node, keyword=_RUST_TYPE_ITEMS[t]))
                if t == "trait_item":
                    body = node.child_by_field_name("body")
                    for member in (body.named_children if body is not None else []):
                        if member.type in _RUST_FN_ITEMS:
                            mname = member.child_by_field_name("name")
                            if mname is not None:
                                defs.add_method(tname, src.text(mname))
            return True
        if t == "impl_item":
            owner = _rust_type_name(src, node.child_by_field_name("type"))
            body = node.child_by_field_name("body")
            if owner and body is not None:
                for member in body.named_children:
                    if member.type in _RUST_FN_ITEMS and not _rust_is_test_gated(src, member, ()):
                        mname = member.child_by_field_name("name")
                        if mname is not None:
                            defs.add_method(owner, src.text(mname))
            return True
        if t in _RUST_FN_ITEMS:
            name_node = node.child_by_field_name("name")
            if name_node is None:
                return True
            modifiers = next((c for c in node.children if c.type == "function_modifiers"), None)
            is_async = modifiers is not None and "async" in src.text(modifiers).split()
            container = None
            for a in reversed(anc):
                if a.type == "impl_item":
                    container = _rust_type_name(src, a.child_by_field_name("type"))
                    break
                if a.type == "trait_item":
                    nn = a.child_by_field_name("name")
                    container = src.text(nn) if nn is not None else None
                    break
                if a.type in ("function_item", "mod_item"):
                    break
            defs.functions.append(_make(
                src, KIND_FUNCTION, src.text(name_node), node, node,
                keyword="fn", params=_strip_delims(_field_text(src, node, "parameters")),
                is_async=is_async, container=container,
                has_body=node.child_by_field_name("body") is not None,
            ))
            return True
        return True

    _walk(root, visit)
    return defs


# ---------------------------------------------------------------------------
# Go
# ---------------------------------------------------------------------------


def _go_receiver_type(src: _Src, receiver) -> Optional[str]:  # noqa: ANN001
    if receiver is None:
        return None
    for param in receiver.named_children:
        if param.type != "parameter_declaration":
            continue
        tnode = param.child_by_field_name("type")
        while tnode is not None and tnode.type in ("pointer_type", "generic_type", "parenthesized_type"):
            if tnode.type == "generic_type":
                tnode = tnode.child_by_field_name("type")
            else:
                tnode = tnode.named_children[0] if tnode.named_children else None
        if tnode is not None and tnode.type == "type_identifier":
            return src.text(tnode)
    return None


def _go_definitions(src: _Src, root) -> Definitions:  # noqa: ANN001
    defs = Definitions()

    def visit(node, anc):  # noqa: ANN001,ANN202
        t = node.type
        if t in ("type_spec", "type_alias"):
            # Every named type (v0.2.102 review LOW-4): structs and interfaces
            # as before, plus `type ID string`, `type H func(…)`, and aliases
            # `type A = B` — methods attach to any of them by receiver.
            tnode = node.child_by_field_name("type")
            name_node = node.child_by_field_name("name")
            if tnode is None or name_node is None:
                return True
            parent = node.parent
            single = parent is not None and parent.type == "type_declaration" and len(
                [c for c in parent.named_children if c.type in ("type_spec", "type_alias")]) == 1
            outer = parent if single else node
            tname = src.text(name_node)
            keyword = {"struct_type": "struct", "interface_type": "interface"}.get(
                tnode.type, "alias" if t == "type_alias" else "type")
            defs.classes.append(_make(src, KIND_CLASS, tname, outer, outer, keyword=keyword))
            if tnode.type == "interface_type":
                for member in tnode.named_children:
                    if member.type in ("method_elem", "method_spec"):
                        mname = member.child_by_field_name("name")
                        if mname is not None:
                            defs.add_method(tname, src.text(mname))
            return True
        if t in ("function_declaration", "method_declaration"):
            name_node = node.child_by_field_name("name")
            if name_node is None:
                return True
            owner = _go_receiver_type(src, node.child_by_field_name("receiver")) if t == "method_declaration" else None
            fname = src.text(name_node)
            if owner:
                defs.add_method(owner, fname)
            defs.functions.append(_make(
                src, KIND_FUNCTION, fname, node, node,
                keyword="func", params=_strip_delims(_field_text(src, node, "parameters")),
                container=owner,
            ))
            return True
        return True

    _walk(root, visit)
    return defs


# ---------------------------------------------------------------------------
# Java / C# (lexical class members)
# ---------------------------------------------------------------------------

_JAVA_CLASS_TYPES = {
    "class_declaration": "class",
    "interface_declaration": "interface",
    "enum_declaration": "enum",
    "record_declaration": "record",
    "annotation_type_declaration": "@interface",
}
_JAVA_FUNC_TYPES = {
    "method_declaration", "constructor_declaration", "compact_constructor_declaration",
    "annotation_type_element_declaration",  # `String value() default "";`
}
_JAVA_SKIP = {"marker_annotation", "annotation"}

_CS_CLASS_TYPES = {
    "class_declaration": "class",
    "interface_declaration": "interface",
    "record_declaration": "record",
    "record_struct_declaration": "record",
    "struct_declaration": "struct",
    "enum_declaration": "enum",
}
#: ``local_function_statement`` — a C# local function, found at any depth like
#: every nested definition; it is never a member of the class's methods list.
_CS_FUNC_TYPES = {"method_declaration", "constructor_declaration", "local_function_statement"}

#: Body wrappers between a type declaration and its members (Java enums nest
#: members one level deeper, in ``enum_body_declarations``).
_MEMBER_BODY_TYPES = frozenset({
    "class_body", "interface_body", "enum_body", "enum_body_declarations",
    "annotation_type_body", "declaration_list", "enum_member_declaration_list",
})


def _member_owner(node):  # noqa: ANN001,ANN202
    """The declaration a member belongs to DIRECTLY: climb the body wrappers;
    whatever sits above them is the owner (``None`` at the top)."""
    parent = node.parent
    while parent is not None and parent.type in _MEMBER_BODY_TYPES:
        parent = parent.parent
    return parent
_CS_SKIP = {"attribute_list"}


def _lexical_container(src: _Src, anc: Tuple, class_types: Dict[str, str]) -> Optional[str]:
    for a in reversed(anc):
        if a.type in class_types:
            nn = a.child_by_field_name("name")
            if nn is not None:
                return src.text(nn)
    return None


def _member_definitions(
    src: _Src, root, class_types: Dict[str, str], func_types: Set[str],  # noqa: ANN001
    skip: Set[str], descend: Set[str], member_extra: Set[str],
) -> Definitions:
    defs = Definitions()
    properties: List[Tuple[str, str]] = []

    def visit(node, anc):  # noqa: ANN001,ANN202
        t = node.type
        if t in class_types:
            name_node = node.child_by_field_name("name")
            if name_node is None:
                return True
            cname = src.text(name_node)
            start = _decl_start(node, skip, descend)
            attrs = [src.text(c) for c in node.children if c.type in skip]
            defs.classes.append(_make(
                src, KIND_CLASS, cname, start, node, keyword=class_types[t], attributes=attrs,
            ))
            return True
        if t in func_types:
            name_node = node.child_by_field_name("name")
            if name_node is None:
                return True
            container = _lexical_container(src, anc, class_types)
            mname = src.text(name_node)
            start = _decl_start(node, skip, descend)
            attrs = [src.text(c) for c in node.children if c.type in skip]
            modifiers = " ".join(
                src.text(c) for c in node.children if c.type in ("modifier", "modifiers")
            ).split()
            body = node.child_by_field_name("body")
            # Membership: only a DIRECT member of the type body counts toward
            # its methods list (a method of an anonymous class, or a local
            # function, does not).
            owner_node = _member_owner(node)
            if owner_node is not None and owner_node.type in class_types and container:
                defs.add_method(container, mname)
            defs.functions.append(_make(
                src, KIND_FUNCTION, mname, start, node,
                keyword="method", params=_strip_delims(_field_text(src, node, "parameters")),
                is_async="async" in modifiers, container=container,
                has_body=body is not None, attributes=attrs,
            ))
            return True
        if t in member_extra:
            name_node = node.child_by_field_name("name")
            owner_node = _member_owner(node)
            if name_node is not None and owner_node is not None and owner_node.type in class_types:
                owner = owner_node.child_by_field_name("name")
                if owner is not None:
                    properties.append((src.text(owner), src.text(name_node)))
            return True
        return True

    _walk(root, visit)
    # Properties are listed AFTER the methods of their class (C# extractor
    # convention: the CLR get_/set_ accessors are real methods).
    for owner, pname in properties:
        defs.add_method(owner, pname)
    return defs


def _java_definitions(src: _Src, root) -> Definitions:  # noqa: ANN001
    return _member_definitions(src, root, _JAVA_CLASS_TYPES, _JAVA_FUNC_TYPES,
                               _JAVA_SKIP, {"modifiers"}, set())


def _csharp_definitions(src: _Src, root) -> Definitions:  # noqa: ANN001
    return _member_definitions(src, root, _CS_CLASS_TYPES, _CS_FUNC_TYPES,
                               _CS_SKIP, set(), {"property_declaration"})


# ---------------------------------------------------------------------------
# C++
# ---------------------------------------------------------------------------

_CPP_CLASS_TYPES = {"class_specifier": "class", "struct_specifier": "struct"}


def _cpp_outer(node):  # noqa: ANN001,ANN202
    parent = node.parent
    if parent is not None and parent.type == "template_declaration":
        return parent
    return node


def _cpp_function_declarator(node):  # noqa: ANN001,ANN202
    """Descend a declarator chain (pointer/reference wrappers) to the
    ``function_declarator``."""
    d = node.child_by_field_name("declarator")
    while d is not None and d.type != "function_declarator":
        nxt = d.child_by_field_name("declarator")
        if nxt is None:
            named = [c for c in d.named_children if c.type.endswith("declarator")]
            nxt = named[0] if named else None
        d = nxt
    return d


def _cpp_name(src: _Src, decl) -> Tuple[Optional[str], Optional[str]]:  # noqa: ANN001
    """(name, qualifier) of a function declarator's ``declarator`` field."""
    target = decl.child_by_field_name("declarator")
    if target is None:
        return None, None
    if target.type == "qualified_identifier":
        scope = target.child_by_field_name("scope")
        name = target.child_by_field_name("name")
        while name is not None and name.type == "qualified_identifier":
            scope = name.child_by_field_name("scope")
            name = name.child_by_field_name("name")
        qual = None
        if scope is not None:
            qual = src.text(scope).split("::")[-1].split("<")[0].strip()
        return (src.text(name) if name is not None else None), qual
    if target.type in ("identifier", "field_identifier", "destructor_name", "operator_name"):
        return src.text(target), None
    return None, None


def _cpp_macro_class(src: _Src, node, defs: Definitions) -> bool:  # noqa: ANN001
    """Recover ``class EXPORT_MACRO Name { … };`` — the shape every shipped
    C++ header uses for symbol visibility. Without the preprocessor the grammar
    reads it as a FUNCTION definition whose return type is ``class
    EXPORT_MACRO`` and whose name is ``Name``; the "function body" is the class
    body. Records the class (named ``Name``, never the macro) and its members,
    and returns True; returns False for any other function definition."""
    spec = node.child_by_field_name("type")
    decl = node.child_by_field_name("declarator")
    body = node.child_by_field_name("body")
    if (spec is None or spec.type not in _CPP_CLASS_TYPES or spec.child_by_field_name("body") is not None
            or decl is None or decl.type != "identifier" or body is None):
        return False
    cname = src.text(decl)
    defs.classes.append(_make(src, KIND_CLASS, cname, node, node, keyword=_CPP_CLASS_TYPES[spec.type]))

    def members(n):  # noqa: ANN001,ANN202
        for child in n.named_children:
            if child.type == "labeled_statement":  # `public:` swallows the next member
                yield from members(child)
            elif child.type in ("declaration", "field_declaration", "function_definition"):
                yield child

    for member in members(body):
        if member.type == "function_definition":
            fd = _cpp_function_declarator(member)
            inline_body = member.child_by_field_name("body")
        else:
            # `void draw() { }` inside the misread body parses as a declaration
            # whose initializer list is the member's inline body.
            init = member.child_by_field_name("declarator")
            fd = init
            inline_body = None
            if init is not None and init.type == "init_declarator":
                fd = init.child_by_field_name("declarator")
                value = init.child_by_field_name("value")
                inline_body = value if value is not None and value.type == "initializer_list" else None
            if fd is not None and fd.type != "function_declarator":
                fd = None
        if fd is None:
            continue
        mname, _q = _cpp_name(src, fd)
        if not mname:
            continue
        defs.add_method(cname, mname)
        if inline_body is not None:
            defs.functions.append(_make(
                src, KIND_FUNCTION, mname, member, member, keyword="function",
                params=_strip_delims(_field_text(src, fd, "parameters")), container=cname,
            ))
    return True


def _cpp_definitions(src: _Src, root) -> Definitions:  # noqa: ANN001
    defs = Definitions()

    def visit(node, anc):  # noqa: ANN001,ANN202
        t = node.type
        if t in _CPP_CLASS_TYPES:
            name_node = node.child_by_field_name("name")
            body = node.child_by_field_name("body")
            if name_node is None or body is None:
                return True
            outer = _cpp_outer(node)
            # A specifier inside a declaration (`struct S {…} s;`) still starts
            # where the declaration does.
            if outer is node and node.parent is not None and node.parent.type in ("declaration", "type_definition"):
                outer = node.parent
            cname = src.text(name_node).split("<")[0].strip()
            defs.classes.append(_make(src, KIND_CLASS, cname, outer, node, keyword=_CPP_CLASS_TYPES[t]))
            for member in body.named_children:
                target = member
                if member.type == "template_declaration":
                    inner = [c for c in member.named_children if c.type in ("function_definition", "declaration", "field_declaration")]
                    target = inner[0] if inner else member
                if target.type in ("field_declaration", "declaration", "function_definition"):
                    fd = _cpp_function_declarator(target)
                    if fd is not None:
                        mname, _q = _cpp_name(src, fd)
                        if mname:
                            defs.add_method(cname, mname)
            return True
        if t == "function_definition" and _cpp_macro_class(src, node, defs):
            return False
        if t == "function_definition":
            fd = _cpp_function_declarator(node)
            if fd is None or node.child_by_field_name("body") is None:
                return True
            name, qual = _cpp_name(src, fd)
            if not name:
                return True
            container = None
            for a in reversed(anc):
                if a.type in _CPP_CLASS_TYPES and a.child_by_field_name("name") is not None:
                    container = src.text(a.child_by_field_name("name")).split("<")[0].strip()
                    break
                if a.type == "function_definition":
                    break
            if qual:
                defs.add_method(qual, name)
            outer = _cpp_outer(node)
            defs.functions.append(_make(
                src, KIND_FUNCTION, name, outer, node,
                keyword="function", params=_strip_delims(_field_text(src, fd, "parameters")),
                container=container, qualifier=qual,
            ))
            return False  # no nested function definitions in C++ (lambdas are not)
        return True

    _walk(root, visit)
    return defs


# ---------------------------------------------------------------------------
# Ruby
# ---------------------------------------------------------------------------


def _ruby_definitions(src: _Src, root) -> Definitions:  # noqa: ANN001
    defs = Definitions()

    def _container(anc: Tuple) -> Optional[str]:
        for a in reversed(anc):
            if a.type in ("class", "module"):
                nn = a.child_by_field_name("name")
                if nn is not None:
                    return src.text(nn).split("::")[-1]
        return None

    def visit(node, anc):  # noqa: ANN001,ANN202
        t = node.type
        if t in ("class", "module"):
            name_node = node.child_by_field_name("name")
            if name_node is None:
                return True
            superclass = node.child_by_field_name("superclass")
            defs.classes.append(_make(
                src, KIND_CLASS, src.text(name_node).split("::")[-1], node, node,
                keyword=t, base=src.text(superclass).lstrip("<").strip() if superclass is not None else None,
            ))
            return True
        if t in ("method", "singleton_method"):
            name_node = node.child_by_field_name("name")
            if name_node is None:
                return True
            container = _container(anc)
            mname = src.text(name_node)
            if container:
                defs.add_method(container, mname)
            defs.functions.append(_make(
                src, KIND_FUNCTION, mname, node, node,
                keyword="def", params=_strip_delims(_field_text(src, node, "parameters")),
                container=container,
            ))
            return True
        return True

    _walk(root, visit)
    return defs


# ---------------------------------------------------------------------------
# Lua (table-OOP classes)
# ---------------------------------------------------------------------------


def _lua_dotted(src: _Src, node) -> Optional[Tuple[str, Optional[str], str]]:  # noqa: ANN001
    """(qualified text, table/qualifier or None, leaf name) for a function
    name: ``f``, ``T.f``, ``T:f``, ``a.b.c``."""
    if node.type == "identifier":
        text = src.text(node)
        return text, None, text
    if node.type in ("dot_index_expression", "method_index_expression"):
        leaf = node.child_by_field_name("field") or node.child_by_field_name("method")
        table = node.child_by_field_name("table")
        if leaf is None or table is None:
            return None
        return src.text(node), src.text(table), src.text(leaf)
    return None


def _lua_definitions(src: _Src, root) -> Definitions:  # noqa: ANN001
    defs = Definitions()
    class_decl: Dict[str, int] = {}
    index_line: Dict[str, int] = {}
    raw_funcs: List[Definition] = []

    def visit(node, anc):  # noqa: ANN001,ANN202
        t = node.type
        if t == "assignment_statement":
            targets = node.child_by_field_name("variable_list") or next(
                (c for c in node.named_children if c.type == "variable_list"), None)
            values = next((c for c in node.named_children if c.type == "expression_list"), None)
            tlist = [c for c in (targets.named_children if targets is not None else [])]
            vlist = [c for c in (values.named_children if values is not None else [])]
            if len(tlist) == 1 and len(vlist) == 1:
                target, value = tlist[0], vlist[0]
                parent = node.parent
                is_local = parent is not None and parent.type == "variable_declaration"
                top_level = parent == root or (
                    parent is not None and is_local and parent.parent == root)
                if (value.type == "table_constructor" and not value.named_children
                        and target.type == "identifier" and top_level and not is_local):
                    name = src.text(target)
                    if name[:1].isupper():
                        class_decl.setdefault(name, _start_line(node))
                if (target.type == "dot_index_expression" and value.type == "identifier"):
                    leaf = target.child_by_field_name("field")
                    table = target.child_by_field_name("table")
                    if (leaf is not None and table is not None and src.text(leaf) == "__index"
                            and table.type == "identifier" and src.text(table) == src.text(value)):
                        index_line.setdefault(src.text(table), _start_line(node))
                if value.type == "function_definition":
                    parts = _lua_dotted(src, target)
                    if parts is not None:
                        qual_text, table_text, leaf = parts
                        outer = parent if (is_local and parent is not None) else node
                        raw_funcs.append(_make(
                            src, KIND_FUNCTION, leaf, outer, outer,
                            keyword="function", params=_strip_delims(_field_text(src, value, "parameters")),
                            qualifier=table_text, tag=qual_text,
                        ))
            return True
        if t == "function_declaration":
            name_node = node.child_by_field_name("name")
            parts = _lua_dotted(src, name_node) if name_node is not None else None
            if parts is None:
                return True
            qual_text, table_text, leaf = parts
            raw_funcs.append(_make(
                src, KIND_FUNCTION, leaf, node, node,
                keyword="function", params=_strip_delims(_field_text(src, node, "parameters")),
                qualifier=table_text, tag=qual_text,
            ))
            return True
        return True

    _walk(root, visit)
    class_names = list(dict.fromkeys(list(class_decl) + list(index_line)))
    for cname in class_names:
        line = class_decl.get(cname) or _lua_local_decl_line(src, root, cname) or index_line.get(cname, 1)
        defs.classes.append(Definition(kind=KIND_CLASS, name=cname, start_line=line, end_line=line, keyword="table"))
    for fn in raw_funcs:
        if fn.qualifier in class_decl or fn.qualifier in index_line:
            defs.add_method(fn.qualifier, fn.name)  # type: ignore[arg-type]
            fn.container = fn.qualifier
        defs.functions.append(fn)
    return defs


def _lua_local_decl_line(src: _Src, root, name: str) -> Optional[int]:  # noqa: ANN001
    """Line of a top-level ``local Name = {…}`` (the class table's declaration)."""
    for stmt in root.named_children:
        if stmt.type != "variable_declaration":
            continue
        assign = next((c for c in stmt.named_children if c.type == "assignment_statement"), None)
        if assign is None:
            continue
        targets = next((c for c in assign.named_children if c.type == "variable_list"), None)
        if targets is not None and any(src.text(c) == name for c in targets.named_children):
            return _start_line(stmt)
    return None


# ---------------------------------------------------------------------------
# Shell (bash grammar)
# ---------------------------------------------------------------------------


def _shell_definitions(src: _Src, root) -> Definitions:  # noqa: ANN001
    defs = Definitions()

    def visit(node, anc):  # noqa: ANN001,ANN202
        if node.type == "function_definition":
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                defs.functions.append(_make(src, KIND_FUNCTION, src.text(name_node), node, node, keyword="function"))
        return True

    _walk(root, visit)
    return defs


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

#: canonical language id -> (grammar id, walker)
_WALKERS: Dict[str, Tuple[str, Callable]] = {
    "javascript": ("javascript", _js_definitions),
    "typescript": ("typescript", _js_definitions),
    "tsx": ("tsx", _js_definitions),
    "rust": ("rust", _rust_definitions),
    "go": ("go", _go_definitions),
    "java": ("java", _java_definitions),
    "csharp": ("csharp", _csharp_definitions),
    "cpp": ("cpp", _cpp_definitions),
    "ruby": ("ruby", _ruby_definitions),
    "lua": ("lua", _lua_definitions),
    "shell": ("shell", _shell_definitions),
}


def supported_definition_languages() -> "frozenset[str]":
    """Language ids :func:`extract_definitions` has a walker for (whether or
    not the grammar is installed in this process)."""
    return frozenset(_WALKERS)


_PARSE_ERROR_LOGGED: Set[Tuple[str, str]] = set()


def _has_parse_error(tree, language: str, origin: str) -> bool:  # noqa: ANN001
    """True when the parse tree contains an ERROR / MISSING node.

    Why the whole file goes to regex rather than unioning regex discovery for
    the ERROR spans: tree-sitter's error recovery does not confine damage to
    the ERROR node — a half-edited signature can make it absorb every later
    declaration into one ERROR subtree, or re-parent them so their ranges are
    wrong without any ERROR node around them. Deciding which tree-sitter rows
    to trust next to an error would be a heuristic of its own; the regex
    extractor is the pre-v0.2.102 behaviour, scans line-by-line, and loses
    only the broken definition itself. A clean file (the common case) keeps
    the exact tree-sitter path. Logged once per file per process."""
    if not tree.root_node.has_error:
        return False
    key = (language, origin)
    if key not in _PARSE_ERROR_LOGGED:
        _PARSE_ERROR_LOGGED.add(key)
        logger.info(
            "codegraph_defs: %s parse error in %s; using the regex extractor "
            "for this file", language, origin or "<source>",
        )
    return True


def extract_definitions(language: str, source_text: str, origin: str = "") -> Optional[Definitions]:
    """Parse ``source_text`` with ``language``'s grammar and return its
    definitions, or ``None`` when the caller must run its regex discovery
    instead: the grammar is unavailable, the file has a parse error
    (:func:`_has_parse_error`), or the walker raised (logged). ``origin`` (the
    file's repo-relative path) only labels the log lines."""
    entry = _WALKERS.get(language)
    if entry is None:
        return None
    grammar, walker = entry
    data = source_text.encode("utf-8", "replace")
    tree = parse(grammar, data)
    if tree is None or _has_parse_error(tree, language, origin):
        return None
    try:
        return walker(_Src(data), tree.root_node)
    except Exception as exc:  # noqa: BLE001 — a walker bug must not lose the file
        logger.warning(
            "codegraph_defs: %s definitions walk failed (%s: %s); using the "
            "regex extractor for this file", language, type(exc).__name__, exc,
        )
        return None


def extract_svelte_script_definitions(
    blocks: Iterable[Tuple[str, bool, int]], content: str, origin: str = "",
) -> Optional[List[Tuple[Definition, bool]]]:
    """Definitions of every Svelte ``<script>`` block, parsed with the
    TypeScript grammar (a superset of the JavaScript Svelte scripts use).

    ``blocks`` is the Svelte extractor's ``(body, is_module, body_offset)``
    list; returned line numbers are FILE lines. ``None`` when the grammar is
    unavailable or any block has a parse error (the whole file then takes the
    regex path). Each item is ``(definition, is_module_scope)``.
    """
    results: List[Tuple[Definition, bool]] = []
    for body, is_module, offset in blocks:
        data = body.encode("utf-8", "replace")
        tree = parse("typescript", data)
        if tree is None or _has_parse_error(tree, "svelte", origin):
            return None
        try:
            defs = _js_definitions(_Src(data), tree.root_node, svelte_module=is_module)
        except Exception as exc:  # noqa: BLE001
            logger.warning("codegraph_defs: svelte script walk failed (%s: %s)", type(exc).__name__, exc)
            return None
        line_offset = content[:offset].count("\n")
        for d in defs.functions:
            # Svelte keeps only TOP-LEVEL arrows bound by `export` (as before);
            # function declarations at any depth and reactive declarations.
            if d.tag == "arrow":
                continue
            d.start_line += line_offset
            d.end_line += line_offset
            results.append((d, is_module))
    results.sort(key=lambda r: (r[0].start_line, r[0].start_col))
    return results
