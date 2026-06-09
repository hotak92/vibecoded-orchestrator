# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for V52-I (v0.2.52): valid_until property gap.

Fix A — Defensive schema-aware filter
    `_collection_has_valid_until(name)` probes a Weaviate collection's
    schema once (lru_cache) and returns True iff the `valid_until`
    property exists. `_stale_filter_for(name, include_stale=...)`
    consults the cache and returns None when the property is missing,
    letting fan-out call sites skip the stale clause for collections
    that would otherwise raise a schema error (shared KG + *_Diagrams
    on existing installs).

Fix B — Permanent schema closure
    `kg_class_definition` and `diagrams_class_definition` now include
    the 4 canonical temporal date properties (created_at, updated_at,
    valid_from, valid_until) at create-time, matching what the
    `_stale_filter` and `days=` filter expect.

    `scripts/migrate-development-temporal-props.sh|ps1` regex extended
    from `_Development$` to cover `_KnowledgeGraph` and `_Diagrams`
    suffixes too, so existing installs upgrade in-place on the next
    `install.py --update`.

Run: pytest tests/test_v52_i_valid_until_gap.py -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
MCP_ROOT = REPO_ROOT / "claude_mcp_servers"
if str(MCP_ROOT) not in sys.path:
    sys.path.insert(0, str(MCP_ROOT))

from vco_lib import project_init  # noqa: E402


# ---------------------------------------------------------------------------
# Fix A — schema-probe cache + schema-aware stale filter
# ---------------------------------------------------------------------------


def _import_server_module():
    """Import the MCP server module on demand.

    The module's import-time work resolves env vars and constructs the
    weaviate client lazily, so import alone is cheap and side-effect-free
    for the symbols we care about (`_collection_has_valid_until`,
    `_stale_filter`, `_stale_filter_for`).
    """
    from weaviate_mcp import server as srv  # type: ignore[import-not-found]
    return srv


def _fake_collection(prop_names):
    """Construct a stub collections-get handle whose `config.get()`
    returns a dataclass-shaped object exposing `properties` as a list
    of objects with a `.name` attribute. This mirrors how the real
    weaviate-python-client surfaces collection config to callers.
    """
    props = [SimpleNamespace(name=n) for n in prop_names]
    config_obj = SimpleNamespace(properties=props)
    coll_obj = SimpleNamespace(config=SimpleNamespace(get=lambda: config_obj))
    return coll_obj


class CollectionHasValidUntilTests(unittest.TestCase):
    """Direct unit tests on the cache function — no Weaviate live."""

    def setUp(self):
        self.srv = _import_server_module()
        # Always start each test from a clean cache so prior fakes don't
        # leak. lru_cache.cache_clear is the canonical reset.
        self.srv._collection_has_valid_until.cache_clear()

    def test_returns_true_when_property_present(self):
        fake_client = SimpleNamespace(
            collections=SimpleNamespace(
                get=lambda name: _fake_collection(
                    ["title", "content", "valid_until", "valid_from"]
                )
            )
        )
        with mock.patch.object(self.srv, "get_weaviate_client", return_value=fake_client):
            self.assertTrue(self.srv._collection_has_valid_until("Foo_KG"))

    def test_returns_false_when_property_missing(self):
        fake_client = SimpleNamespace(
            collections=SimpleNamespace(
                get=lambda name: _fake_collection(["title", "content"])
            )
        )
        with mock.patch.object(self.srv, "get_weaviate_client", return_value=fake_client):
            self.assertFalse(self.srv._collection_has_valid_until("Foo_Diagrams"))

    def test_returns_false_when_probe_raises(self):
        def _raise(name):
            raise RuntimeError("Weaviate unreachable")

        fake_client = SimpleNamespace(
            collections=SimpleNamespace(get=_raise)
        )
        with mock.patch.object(self.srv, "get_weaviate_client", return_value=fake_client):
            # Conservative default: probe failure → skip the filter
            # rather than schema-error every query.
            self.assertFalse(self.srv._collection_has_valid_until("Unreachable"))

    def test_empty_collection_name_returns_false(self):
        # Defensive null-check: empty string must not hit Weaviate.
        with mock.patch.object(
            self.srv,
            "get_weaviate_client",
            side_effect=AssertionError("must not be called for empty name"),
        ):
            self.assertFalse(self.srv._collection_has_valid_until(""))

    def test_result_is_cached(self):
        call_count = {"n": 0}

        def _get(name):
            call_count["n"] += 1
            return _fake_collection(["valid_until"])

        fake_client = SimpleNamespace(collections=SimpleNamespace(get=_get))
        with mock.patch.object(self.srv, "get_weaviate_client", return_value=fake_client):
            self.srv._collection_has_valid_until("Cached_KG")
            self.srv._collection_has_valid_until("Cached_KG")
            self.srv._collection_has_valid_until("Cached_KG")
        self.assertEqual(
            call_count["n"], 1,
            "expected lru_cache to short-circuit repeat probes for same name",
        )

    def test_reset_clears_cache(self):
        call_count = {"n": 0}

        def _get(name):
            call_count["n"] += 1
            return _fake_collection(["valid_until"])

        fake_client = SimpleNamespace(collections=SimpleNamespace(get=_get))
        with mock.patch.object(self.srv, "get_weaviate_client", return_value=fake_client):
            self.srv._collection_has_valid_until("Reset_KG")
            self.srv._reset_valid_until_cache()
            self.srv._collection_has_valid_until("Reset_KG")
        self.assertEqual(
            call_count["n"], 2,
            "reset helper must invalidate the lru_cache so post-migration "
            "schema state is re-probed",
        )


class StaleFilterForTests(unittest.TestCase):
    """Schema-aware wrapper around the original `_stale_filter`."""

    def setUp(self):
        self.srv = _import_server_module()
        self.srv._collection_has_valid_until.cache_clear()

    def test_returns_none_when_property_missing(self):
        with mock.patch.object(
            self.srv, "_collection_has_valid_until", return_value=False
        ):
            self.assertIsNone(self.srv._stale_filter_for("NoValidUntil_Diagrams"))

    def test_returns_filter_when_property_present(self):
        with mock.patch.object(
            self.srv, "_collection_has_valid_until", return_value=True
        ):
            result = self.srv._stale_filter_for("HasIt_KG")
            self.assertIsNotNone(
                result,
                "expected a Weaviate Filter; got None — wrapper failed "
                "to delegate to _stale_filter",
            )

    def test_include_stale_short_circuits_to_none(self):
        # Even with the property present, include_stale=True must bypass
        # the filter (audit / history queries opt out of staleness pruning).
        with mock.patch.object(
            self.srv, "_collection_has_valid_until", return_value=True
        ):
            self.assertIsNone(
                self.srv._stale_filter_for("HasIt_KG", include_stale=True),
            )

    def test_empty_collection_name_returns_none(self):
        # Defensive: empty name treated as missing-property.
        self.assertIsNone(self.srv._stale_filter_for(""))

    def test_bare_stale_filter_still_works(self):
        # The non-schema-aware `_stale_filter` is kept for backward
        # compatibility with any caller that knows the collection has
        # the property; ensure it still returns a Filter shape.
        result = self.srv._stale_filter(include_stale=False)
        self.assertIsNotNone(result)
        # include_stale=True still bypasses
        self.assertIsNone(self.srv._stale_filter(include_stale=True))


# ---------------------------------------------------------------------------
# Fix B — class-definition temporal props (schema closure)
# ---------------------------------------------------------------------------


class KGClassDefinitionTemporalPropsTests(unittest.TestCase):
    """The KG class definition must ship with the 4 canonical temporal
    date props so fresh installs don't need a post-create migrate.
    """

    REQUIRED_TEMPORAL = {"created_at", "updated_at", "valid_from", "valid_until"}

    def test_kg_class_has_all_temporal_props(self):
        schema = project_init.kg_class_definition("Foo_KG")
        prop_names = {p["name"] for p in schema["properties"]}
        missing = self.REQUIRED_TEMPORAL - prop_names
        self.assertFalse(
            missing,
            f"KG class definition is missing temporal props {missing}; "
            f"present: {sorted(prop_names)}",
        )

    def test_kg_class_temporal_props_are_dates(self):
        schema = project_init.kg_class_definition("Foo_KG")
        by_name = {p["name"]: p for p in schema["properties"]}
        for prop_name in self.REQUIRED_TEMPORAL:
            with self.subTest(prop=prop_name):
                self.assertEqual(
                    by_name[prop_name]["dataType"], ["date"],
                    f"{prop_name} must be a date dataType to support "
                    f"`Filter.by_property(...).greater_than(datetime)`",
                )


class DiagramsClassDefinitionTemporalPropsTests(unittest.TestCase):
    """Diagrams class definition gains validity date props (valid_from /
    valid_until) so the universal stale filter doesn't schema-error on
    diagram collections.

    Note: the existing diagrams schema also has `created_at` and
    `updated_at` as INT (unix epoch) — those are kept as-is for the
    indexer's existing write path (`vco_lib/diagram_indexer.py`
    writes ints from `int(time.time())`). The date-typed `valid_from` /
    `valid_until` are added alongside; they default to None on rows the
    indexer writes (no temporal validity tracking yet on diagrams).
    """

    def test_diagrams_class_has_validity_date_props(self):
        schema = project_init.diagrams_class_definition("Foo_Diagrams")
        prop_names = {p["name"] for p in schema["properties"]}
        for required in ("valid_from", "valid_until"):
            with self.subTest(prop=required):
                self.assertIn(
                    required, prop_names,
                    f"Diagrams class definition is missing validity prop "
                    f"'{required}'; present: {sorted(prop_names)}",
                )

    def test_diagrams_class_validity_props_are_dates(self):
        schema = project_init.diagrams_class_definition("Foo_Diagrams")
        by_name = {p["name"]: p for p in schema["properties"]}
        for required in ("valid_from", "valid_until"):
            with self.subTest(prop=required):
                self.assertEqual(
                    by_name[required]["dataType"], ["date"],
                    f"{required} must be date-typed for the stale filter "
                    f"to work",
                )

    def test_diagrams_class_retains_int_timestamps(self):
        """The pre-existing INT timestamps (created_at / updated_at,
        used by `vco_lib/diagram_indexer.py::_weaviate_upsert`) must
        remain present and INT-typed — Fix B is purely additive,
        not a rename / retype."""
        schema = project_init.diagrams_class_definition("Foo_Diagrams")
        by_name = {p["name"]: p for p in schema["properties"]}
        self.assertIn("created_at", by_name)
        self.assertIn("updated_at", by_name)
        self.assertEqual(by_name["created_at"]["dataType"], ["int"])
        self.assertEqual(by_name["updated_at"]["dataType"], ["int"])


class MigrateScriptRegexTests(unittest.TestCase):
    """The migrate shell script must match `_KnowledgeGraph` and
    `_Diagrams` collections in addition to `_Development`.

    Verified by literal-substring inspection so tests don't require
    a live Weaviate. The script is idempotent: re-running on an
    already-migrated collection is a no-op (per-property presence
    check before POST).
    """

    BASH_PATH = REPO_ROOT / "scripts" / "migrate-development-temporal-props.sh"
    PS1_PATH = REPO_ROOT / "scripts" / "migrate-development-temporal-props.ps1"

    def test_bash_regex_covers_kg_and_diagrams(self):
        text = self.BASH_PATH.read_text(encoding="utf-8")
        # The jq filter selects collections whose name matches the
        # extended regex.  Either grouping syntax is acceptable as long
        # as all three suffixes are present.
        self.assertIn("_KnowledgeGraph", text)
        self.assertIn("_Development", text)
        self.assertIn("_Diagrams", text)
        # Spot-check that the regex is the new pattern, not the old one.
        # We don't pin the exact form (regex grouping syntax varies),
        # only that the three suffixes co-occur on the SAME regex line.
        regex_line = next(
            ln for ln in text.splitlines() if "select(test(" in ln
        )
        for suffix in ("_KnowledgeGraph", "_Development", "_Diagrams"):
            self.assertIn(
                suffix, regex_line,
                f"migrate-development-temporal-props.sh regex line "
                f"missing {suffix}; line was: {regex_line!r}",
            )

    def test_ps1_match_covers_kg_and_diagrams(self):
        text = self.PS1_PATH.read_text(encoding="utf-8")
        self.assertIn("_KnowledgeGraph", text)
        self.assertIn("_Development", text)
        self.assertIn("_Diagrams", text)
        # PowerShell uses -match with a regex literal. Find the CODE line
        # (the one inside the `if (...)` discrimination), not a docstring
        # comment that happens to mention `-match`. The code line has
        # `-match` AND the dollar-anchor regex `$'` AND is not a comment
        # (PowerShell single-line comments start with `#`).
        match_line = next(
            ln for ln in text.splitlines()
            if "-match" in ln
            and "$'" in ln  # regex literal closes with $'
            and not ln.strip().startswith("#")
        )
        for suffix in ("_KnowledgeGraph", "_Development", "_Diagrams"):
            self.assertIn(
                suffix, match_line,
                f"migrate-development-temporal-props.ps1 -match line "
                f"missing {suffix}; line was: {match_line!r}",
            )


if __name__ == "__main__":
    unittest.main()
