# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""``vco_lib.jsonc_edit`` — the ONE JSONC reader/editor every settings writer uses.

Pure text in, text out. The property under test is always the same: the
result parses to exactly the requested settings, and every byte the edit did
not have to touch — comments, CRLF, key order, indentation, trailing commas —
is where it was. What cannot be done that way is refused, not approximated.
"""
from __future__ import annotations

import pytest

from vco_lib import jsonc_edit as j

ENV = "claudeCode.environmentVariables"

DOC = (
    "{\n"
    "    // my editor setup\n"
    '    "editor.fontSize": 13,\n'
    f'    "{ENV}": {{\n'
    '        "ANTHROPIC_BASE_URL": "http://127.0.0.1:11436", // the gateway\n'
    '        "ANTHROPIC_MODEL": "claude-opus-5[1m]",\n'
    '        "MY_KEY": "a // not a comment, /* nor this */",\n'
    "    },\n"
    "    /* trailing block comment */\n"
    "}\n"
)


def _with(doc: str, **changes):
    new = j.loads(doc)
    for path, value in changes.items():
        new[path] = value
    return new


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------


def test_loads_reads_comments_and_trailing_commas_outside_strings():
    parsed = j.loads(DOC)
    assert parsed[ENV]["ANTHROPIC_MODEL"] == "claude-opus-5[1m]"
    assert parsed[ENV]["MY_KEY"] == "a // not a comment, /* nor this */"
    assert j.is_strict_json(DOC) is False
    assert j.is_strict_json('{"a": 1}') is True


@pytest.mark.parametrize("bad", ['{"a": 1', '{"a": /* open', '{"a": "x}', "{ @ }", "{,}"])
def test_loads_still_rejects_what_is_not_jsonc(bad: str):
    with pytest.raises(ValueError):
        j.loads(bad)


# ---------------------------------------------------------------------------
# removing
# ---------------------------------------------------------------------------


def test_removing_a_middle_member_touches_only_its_line():
    new = j.loads(DOC)
    del new[ENV]["ANTHROPIC_MODEL"]
    out = j.rewrite_preserving(DOC, new)
    assert out == DOC.replace('        "ANTHROPIC_MODEL": "claude-opus-5[1m]",\n', "")


def test_removing_the_last_member_takes_its_preceding_comma_and_keeps_comments():
    doc = f'{{\n  "{ENV}": {{\n    "A": "1", /* keep me */\n    "B": "2"\n  }}\n}}\n'
    new = j.loads(doc)
    del new[ENV]["B"]
    assert j.rewrite_preserving(doc, new) == f'{{\n  "{ENV}": {{\n    "A": "1" /* keep me */\n  }}\n}}\n'


def test_crlf_and_a_trailing_line_comment_survive_a_removal():
    doc = f'{{\r\n  "{ENV}": {{\r\n    "B": "2" // pinned\r\n  }},\r\n}}\r\n'
    new = j.loads(doc)
    del new[ENV]["B"]
    out = j.rewrite_preserving(doc, new)
    assert "// pinned" in out and '"B"' not in out
    assert "\n" not in out.replace("\r\n", ""), "no bare LF introduced"


def test_a_same_named_key_in_another_object_is_not_the_target():
    doc = f'{{"other": {{"{ENV}": {{"B": "x"}}}}, "{ENV}": {{"B": "y"}}, // c\n}}'
    new = j.loads(doc)
    del new[ENV]["B"]
    out = j.rewrite_preserving(doc, new)
    assert j.loads(out) == {"other": {ENV: {"B": "x"}}, ENV: {}}
    assert "// c" in out


def test_removing_a_whole_top_level_member():
    new = j.loads(DOC)
    del new[ENV]
    out = j.rewrite_preserving(DOC, new)
    assert j.loads(out) == {"editor.fontSize": 13}
    assert "// my editor setup" in out and "/* trailing block comment */" in out


# ---------------------------------------------------------------------------
# setting
# ---------------------------------------------------------------------------


def test_replacing_a_value_changes_only_that_value():
    new = j.loads(DOC)
    new[ENV]["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:11460"
    out = j.rewrite_preserving(DOC, new)
    assert out == DOC.replace("http://127.0.0.1:11436", "http://127.0.0.1:11460")


def test_inserting_after_a_trailing_comma_keeps_that_style():
    new = j.loads(DOC)
    new[ENV]["NEW_KEY"] = "v"
    out = j.rewrite_preserving(DOC, new)
    assert out == DOC.replace(
        '        "MY_KEY": "a // not a comment, /* nor this */",\n',
        '        "MY_KEY": "a // not a comment, /* nor this */",\n        "NEW_KEY": "v",\n',
    )


def test_inserting_after_a_last_member_with_a_line_comment():
    doc = '{\n    "a": 1 // note\n}\n'
    out = j.rewrite_preserving(doc, {"a": 1, "b": True})
    assert out == '{\n    "a": 1, // note\n    "b": true\n}\n'


def test_inserting_into_an_inline_object():
    assert j.rewrite_preserving('{"a": 1} // x\n', {"a": 1, "b": "c"}) == '{"a": 1, "b": "c"} // x\n'


def test_inserting_into_an_empty_object_and_creating_a_missing_block():
    doc = "{\n    // nothing yet\n}\n"
    out = j.rewrite_preserving(doc, {ENV: {"A": "1"}, "flag": True})
    assert out.startswith("{\n    // nothing yet\n") or out.startswith("{\n")
    assert "// nothing yet" in out
    assert j.loads(out) == {ENV: {"A": "1"}, "flag": True}


def test_crlf_is_kept_on_inserted_lines():
    doc = '{\r\n    "a": 1\r\n}\r\n'
    out = j.rewrite_preserving(doc, {"a": 1, ENV: {"A": "1"}})
    assert "\n" not in out.replace("\r\n", "")
    assert j.loads(out) == {"a": 1, ENV: {"A": "1"}}


def test_no_change_is_no_edit():
    assert j.rewrite_preserving(DOC, j.loads(DOC)) == DOC


# ---------------------------------------------------------------------------
# refusing
# ---------------------------------------------------------------------------


def test_a_duplicate_key_is_refused():
    doc = '{\n  "a": 1,\n  "a": 2,\n  "b": 3,\n}\n'
    with pytest.raises(j.JsoncEditRefused) as info:
        j.rewrite_preserving(doc, {"a": 5, "b": 3})
    assert info.value.reason == "jsonc_duplicate_key"


def test_replacing_a_value_that_holds_comments_is_refused():
    doc = '{\n  "deep": {"x": {"y": 1 /* why */}},\n}\n'
    with pytest.raises(j.JsoncEditRefused) as info:
        j.rewrite_preserving(doc, {"deep": {"x": {"y": 2}}})
    assert info.value.reason == "jsonc_comment_in_replaced_value"


def test_not_jsonc_is_refused():
    with pytest.raises(j.JsoncEditRefused) as info:
        j.rewrite_preserving('{"a": 1', {"a": 2})
    assert info.value.reason == "not_jsonc"


def test_an_edit_that_does_not_reparse_as_requested_is_refused(monkeypatch):
    """The verification is what makes the editor safe: a wrong edit never
    comes back as text."""
    monkeypatch.setattr(j, "_set", lambda text, where, key, value: text)
    with pytest.raises(j.JsoncEditRefused) as info:
        j.rewrite_preserving(DOC, _with(DOC, **{"editor.fontSize": 14}))
    assert info.value.reason == "jsonc_edit_unverified"


# ---------------------------------------------------------------------------
# layout (review F16): an emptied object collapses; inline stays inline
# ---------------------------------------------------------------------------


def test_removing_the_only_member_collapses_the_object():
    doc = f'{{\n  "{ENV}": {{\n    "A": "1"\n  }},\n  // c\n}}\n'
    assert j.rewrite_preserving(doc, {ENV: {}}) == f'{{\n  "{ENV}": {{}},\n  // c\n}}\n'


def test_an_emptied_object_holding_a_comment_keeps_it():
    doc = f'{{\n  "{ENV}": {{ /* keep */\n    "A": "1"\n  }}\n}}\n'
    out = j.rewrite_preserving(doc, {ENV: {}})
    assert "/* keep */" in out and j.loads(out) == {ENV: {}}


def test_a_value_inserted_into_an_inline_object_stays_on_one_line():
    assert j.rewrite_preserving("{}", {"a": {"b": 1}}) == '{"a": {"b": 1}}'
    assert j.rewrite_preserving('{"x": {}} // k\n', {"x": {"b": [1, 2]}}) == '{"x": {"b": [1, 2]}} // k\n'


def test_a_value_inserted_into_a_multiline_object_is_indented():
    out = j.rewrite_preserving('{\n    "a": 1\n}\n', {"a": 1, "b": {"c": 2}})
    assert out == '{\n    "a": 1,\n    "b": {\n        "c": 2\n    }\n}\n'


# ---------------------------------------------------------------------------
# the file-level pair the workspace-settings helpers use
# ---------------------------------------------------------------------------


def test_load_object_reads_jsonc_and_rejects_the_rest(tmp_path):
    good = tmp_path / "a.json"
    good.write_bytes(DOC.replace("\n", "\r\n").encode("utf-8"))
    loaded = j.load_object(good)
    assert loaded is not None
    data, raw = loaded
    assert data[ENV]["ANTHROPIC_MODEL"] == "claude-opus-5[1m]"
    assert "\r\n" in raw, "read without newline translation, so CRLF is kept"
    for name, text in (("list.json", "[1]"), ("bad.json", '{"a": 1')):
        path = tmp_path / name
        path.write_text(text, encoding="utf-8")
        assert j.load_object(path) is None
    assert j.load_object(tmp_path / "missing.json") is None


def test_dumps_preserving_strict_vs_jsonc():
    assert j.dumps_preserving('{"a": 1}', {"a": 1, "b": 2}, indent=2) == '{\n  "a": 1,\n  "b": 2\n}\n'
    out = j.dumps_preserving(DOC, {**j.loads(DOC), "b": 2}, indent=2)
    assert out is not None and "// my editor setup" in out
    dup = '{\n  "a": 1,\n  "a": 2,\n}\n'
    assert j.dumps_preserving(dup, {"a": 3}, indent=2) is None
