# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.94: ``repair_kg_typed_links.py`` must be able to build its own query.

The script's paging query interpolated ``{_graphql_fields}`` — lowercase, a
name that has never been defined anywhere in the file (the constant is
``_GRAPHQL_FIELDS``). So *every* invocation of this repair tool raised
``NameError: name '_graphql_fields' is not defined`` inside
``_fetch_all_objects``, before a single request left the process. Nothing
caught it: the query text lived inline in a network-calling loop, so no test
could reach it without a live Weaviate.

These tests pin the query text itself — the property list it asks for has to
match the properties the repair loop then reads off each row, or the script
"repairs" rows whose ``title`` / ``file_path`` / ``typed_links`` all come back
``None``.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "claude_mcp_servers" / "scripts" / "repair_kg_typed_links.py"

#: The properties ``_repair_collection`` reads off every fetched row:
#: ``obj.get("typed_links")`` (the thing being repaired), ``obj.get("title")``
#: and ``obj.get("file_path")`` (the log label). Anything missing here comes
#: back absent and the row is mis-classified.
REQUIRED_PROPERTIES = ("title", "file_path", "typed_links")


def _load_script():
    """Import the standalone script by path (it is not in a package)."""
    spec = importlib.util.spec_from_file_location("_repair_kg_typed_links", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_query_builds_without_a_nameerror():
    """The regression: building a page query must not raise.

    Red on the pre-fix source (``NameError: _graphql_fields``).
    """
    mod = _load_script()
    query = mod._build_page_query("MyProject_KnowledgeGraph")
    assert isinstance(query, str) and query.strip()


def test_query_requests_every_property_the_repair_loop_reads():
    mod = _load_script()
    query = mod._build_page_query("MyProject_KnowledgeGraph")
    missing = [p for p in REQUIRED_PROPERTIES if p not in query]
    assert not missing, (
        "the GraphQL page query does not ask Weaviate for "
        f"{missing} — _repair_collection reads those off each row, so they "
        "would come back absent and every row would look empty. They come "
        "from _GRAPHQL_FIELDS; keep that the single place the list is written."
    )
    # `_additional { id }` is what supplies the uuid used for the write-back
    # (`obj["_additional"]["id"]`) and the paging cursor.
    assert "_additional" in query and "id" in query


def test_graphql_fields_constant_is_the_single_source():
    """The property list is written once and interpolated, not duplicated."""
    mod = _load_script()
    assert set(mod._GRAPHQL_FIELDS.split()) == set(REQUIRED_PROPERTIES)
    query = mod._build_page_query("SomeKG")
    assert mod._GRAPHQL_FIELDS in query, (
        "the query must interpolate _GRAPHQL_FIELDS itself — a second, "
        "hand-written copy of the property list would drift from it."
    )


def test_collection_name_and_page_size_are_interpolated():
    mod = _load_script()
    query = mod._build_page_query("Legacy_KnowledgeGraph")
    assert "Legacy_KnowledgeGraph(" in query
    assert f"limit: {mod._PAGE_SIZE}" in query


def test_after_cursor_is_emitted_only_when_paging():
    """First page has no `after:`; subsequent pages carry the cursor."""
    mod = _load_script()
    first = mod._build_page_query("KG")
    assert "after:" not in first

    nxt = mod._build_page_query("KG", "0195f4d2-dead-beef-cafe-000000000001")
    assert 'after: "0195f4d2-dead-beef-cafe-000000000001"' in nxt
