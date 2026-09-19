# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Windows, families and the latest-only picker (v0.2.95).

Three owner requirements land here and each has a way of being wrong that a
green suite would otherwise hide.

**"Show only the latest version of each family."** The failure is a filter
that looks right on the shipped list and silently swallows a family it
mis-parsed — so the tests name the exact groupings, including the three that
are NOT one family with the one they resemble (a ``-flash``, a ``-turbo`` and
an ``-air`` variant each stand alone), and assert the withheld ids are
REPORTED rather than merely gone.

**"Auto-update: a new model inherits at least the previous one's window."**
The failure is inheritance that runs the wrong way, or across a boundary. So
the inheritance tests come in pairs: the act AND the leave-alone — older does
not inherit from newer, one family does not inherit from another, one vendor
does not inherit from another, and an inherited figure never becomes the
source of a further inheritance.

**"Never guess the vendor's windows — the table is authoritative."** The
table beats upstream, a tombstone beats the table, and an id nobody names
reads ``unverified`` rather than plausible.

Underneath all three sits the fact the design was corrected on
(code.claude.com/docs, verified 2026-09-16): Claude Code reads ``id``,
``display_name`` and ``description`` from a gateway's ``/v1/models`` and
NOTHING else, and budgets context by the ID — ``[1m]`` means 1M, anything
else behind a gateway means 200K. So the ``[1m]`` row is a decision with a
cost attached, and ``description`` is the only place a row can say so.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import unittest
from unittest import mock

from model_router import catalog as cat
from model_router import config as cfg
from model_router.context_table import ContextTable, ModelContext, load_seed
from model_router.model_family import parse_model_id
from model_router.routing import ONE_M_SUFFIX
from model_router.vendors import ANTHROPIC_FAMILY, VENDORS, AnthropicFamily, Vendor

from tests.test_model_router_catalog import ACME, ACME_FAMILY, _Clock, table_of
from tests.test_model_router_server import GatewayTestBase


def entry(
    model_id: str,
    *,
    window: int | None = None,
    max_tokens: int | None = None,
    created_at: str | None = None,
) -> cat.CatalogEntry:
    """An upstream row, with or without the window upstream would state."""
    return cat.CatalogEntry(
        id=model_id,
        display_name=model_id,
        max_input_tokens=window,
        max_tokens=max_tokens,
        created_at=created_at,
    )


def resolve_all(
    entries: list[cat.CatalogEntry], table: ContextTable,
) -> dict[str, cat.WindowResolution]:
    """Windows for one upstream's rows, floors included."""
    parts = {e.id: parse_model_id(e.id) for e in entries}
    return cat.resolve_family_windows(entries, table=table, parts=parts)


class ModelIdParsingTests(unittest.TestCase):
    """The nine shapes both upstreams actually publish, plus the edges.

    Each is spelled out rather than generated: the point of the rule is that
    it gets THESE right, and a generator would only re-derive the rule it is
    supposed to be checking.
    """

    CASES = (
        # id, family, version, date, one_m
        ("claude-fable-5-1", "claude-fable", (5, 1), None, False),
        ("claude-opus-4-8", "claude-opus", (4, 8), None, False),
        ("claude-3-7-sonnet-20250219", "claude-sonnet", (3, 7), "20250219", False),
        ("claude-haiku-4-5-20251001", "claude-haiku", (4, 5), "20251001", False),
        ("glm-5.3", "glm", (5, 3), None, False),
        ("glm-5.3-flash", "glm-flash", (5, 3), None, False),
        ("glm-5-turbo", "glm-turbo", (5,), None, False),
        ("glm-4.5-air", "glm-air", (4, 5), None, False),
        ("claude-gw/glm-5.3[1m]", "glm", (5, 3), None, True),
    )

    def test_every_shipped_id_shape_parses_as_documented(self) -> None:
        for model_id, family, version, date, one_m in self.CASES:
            with self.subTest(model=model_id):
                parts = parse_model_id(model_id)
                self.assertEqual(parts.family, family)
                self.assertEqual(parts.version, version)
                self.assertEqual(parts.date, date)
                self.assertEqual(parts.one_m, one_m)

    def test_the_namespace_and_the_suffix_are_both_removed_from_bare_id(self) -> None:
        """``bare_id`` is the spelling the upstream and the table both key on."""
        self.assertEqual(parse_model_id("claude-gw/glm-5.3[1m]").bare_id, "glm-5.3")
        self.assertEqual(parse_model_id("claude-opus-5").bare_id, "claude-opus-5")

    def test_an_eight_digit_segment_is_a_date_and_not_a_version(self) -> None:
        """Read as a version it would outrank every sibling by 20 million."""
        dated = parse_model_id("claude-haiku-4-5-20251001")
        plain = parse_model_id("claude-haiku-4-6")
        self.assertEqual(dated.version, (4, 5))
        self.assertLess(dated.version, plain.version)

    def test_a_seven_or_nine_digit_segment_is_still_a_version(self) -> None:
        """The rule is EXACTLY eight, so a long build number is not a date."""
        self.assertEqual(parse_model_id("acme-1234567").version, (1234567,))
        self.assertIsNone(parse_model_id("acme-1234567").date)
        self.assertEqual(parse_model_id("acme-123456789").version, (123456789,))

    def test_ids_with_nothing_to_say_parse_to_nothing_rather_than_raising(self) -> None:
        """No id's SHAPE may fail a catalog fetch."""
        for model_id in ("", "   ", "---", "//"):
            with self.subTest(model=model_id):
                parts = parse_model_id(model_id)
                self.assertEqual(parts.version, ())
                self.assertIsNone(parts.date)

    def test_an_id_with_no_digits_is_its_own_family(self) -> None:
        """So two unversioned ids never collapse into one picker row."""
        self.assertNotEqual(
            parse_model_id("acme-large").family, parse_model_id("acme-small").family,
        )


class WindowResolutionOrderTests(unittest.TestCase):
    """Tombstone > table > upstream > family floor > nothing."""

    def test_a_tombstone_beats_every_other_source(self) -> None:
        """A deletion anything downstream could undo is not a deletion."""
        answer = cat.resolve_window(
            entry("acme-9", window=cat.ONE_M_WINDOW),
            table=table_of({"acme-9": cat.ONE_M_WINDOW}, tombstones=("acme-9",)),
            family_floor=cat.FamilyFloor("acme-8", cat.ONE_M_WINDOW, 1),
        )
        self.assertEqual(answer.source, cat.WINDOW_DELETED)
        self.assertIsNone(answer.window)

    def test_the_table_beats_upstream(self) -> None:
        answer = cat.resolve_window(
            entry("acme-9", window=cat.ONE_M_WINDOW),
            table=table_of({"acme-9": 128_000}),
            family_floor=None,
        )
        self.assertEqual(answer.source, cat.WINDOW_TABLE)
        self.assertEqual(answer.window, 128_000)

    def test_the_table_matches_the_exact_bare_id_and_nothing_near_it(self) -> None:
        """The 5x-misreport rule, at the layer that publishes the advert."""
        table = table_of({"acme-5": cat.ONE_M_WINDOW})
        near = cat.resolve_window(entry("acme-5-preview"), table=table, family_floor=None)
        self.assertEqual(near.source, cat.WINDOW_UNKNOWN)
        exact = cat.resolve_window(entry("acme-5"), table=table, family_floor=None)
        self.assertEqual(exact.source, cat.WINDOW_TABLE)

    def test_a_table_row_is_found_through_the_namespace_and_the_suffix(self) -> None:
        """A published id is namespaced; the table keys on the vendor's own."""
        answer = cat.resolve_window(
            entry("claude-gw/acme-5[1m]"),
            table=table_of({"acme-5": cat.ONE_M_WINDOW}),
            family_floor=None,
        )
        self.assertEqual(answer.source, cat.WINDOW_TABLE)

    def test_upstream_answers_when_no_table_row_names_the_id(self) -> None:
        answer = cat.resolve_window(
            entry("acme-9", window=512_000, max_tokens=64_000),
            table=table_of(),
            family_floor=None,
        )
        self.assertEqual(answer.source, cat.WINDOW_UPSTREAM)
        self.assertEqual((answer.window, answer.max_output), (512_000, 64_000))

    def test_an_id_nobody_names_is_unknown_and_not_a_plausible_default(self) -> None:
        answer = cat.resolve_window(
            entry("acme-9"), table=table_of(), family_floor=None,
        )
        self.assertEqual(answer.source, cat.WINDOW_UNKNOWN)
        self.assertIsNone(answer.window)
        self.assertIsNone(answer.max_output)

    def test_a_damaged_table_row_reads_unverified_rather_than_zero(self) -> None:
        """Believed whole: a table that says nothing usable says nothing."""
        answer = cat.resolve_window(
            entry("acme-9", window=cat.ONE_M_WINDOW),
            table=ContextTable(
                rows={
                    "acme-9": ModelContext(
                        model_id="acme-9", vendor="acme", context_window=0,
                        max_output=0, window_1m=False,
                        source="https://docs.acme.example/9",
                    )
                },
                source="test", path=None,
            ),
            family_floor=None,
        )
        self.assertEqual(answer.source, cat.WINDOW_TABLE)
        self.assertIsNone(answer.window)

    def test_a_boolean_from_upstream_is_not_a_one_token_window(self) -> None:
        """``bool`` is an ``int``; relaying ``true`` as 1 would be a wrong
        number published with the confidence of a right one."""
        rows = cat.CatalogService._parse_models(
            {"data": [{"id": "acme-9", "max_input_tokens": True}]},
        )
        self.assertIsNone(rows[0].max_input_tokens)


class FamilyFloorTests(unittest.TestCase):
    """Inheritance: newer from older, inside one family, from verified only."""

    def test_a_new_model_inherits_the_previous_version_window(self) -> None:
        """The auto-update requirement: a model that ships tomorrow is not
        advertised at the client's default merely because no file names it."""
        rows = [entry("acme-5", window=cat.ONE_M_WINDOW), entry("acme-6")]
        resolved = resolve_all(rows, table_of())
        self.assertEqual(
            resolved["acme-6"].source, f"{cat.WINDOW_INHERITED_PREFIX}acme-5",
        )
        self.assertEqual(resolved["acme-6"].window, cat.ONE_M_WINDOW)

    def test_an_older_model_does_not_inherit_from_a_newer_one(self) -> None:
        """The leave-alone half. Backwards inheritance would lend a window to
        a model that never had it."""
        rows = [entry("acme-6", window=cat.ONE_M_WINDOW), entry("acme-5")]
        self.assertEqual(
            resolve_all(rows, table_of())["acme-5"].source, cat.WINDOW_UNKNOWN,
        )

    def test_inheritance_does_not_cross_families(self) -> None:
        rows = [entry("acme-5", window=cat.ONE_M_WINDOW), entry("acme-6-mini")]
        resolved = resolve_all(rows, table_of())
        self.assertEqual(resolved["acme-6-mini"].source, cat.WINDOW_UNKNOWN)

    def test_a_variant_inherits_from_its_own_variant_line(self) -> None:
        """Positive control for the test above: ``-mini`` is a family, and a
        newer ``-mini`` does inherit from an older one."""
        rows = [
            entry("acme-5-mini", window=cat.ONE_M_WINDOW),
            entry("acme-6-mini"),
        ]
        resolved = resolve_all(rows, table_of())
        self.assertEqual(
            resolved["acme-6-mini"].source,
            f"{cat.WINDOW_INHERITED_PREFIX}acme-5-mini",
        )

    def test_inheritance_does_not_cross_vendors(self) -> None:
        """Two vendors may both ship a family called the same thing. The
        catalog resolves one upstream at a time, so a floor cannot span them
        — driven through the real ``union`` rather than the helper, because
        the separation IS the call structure."""
        service = _service_with(
            first_payload={"data": [{"id": "claude-shared-5", "max_input_tokens": cat.ONE_M_WINDOW}]},
            vendor_payload={"data": [{"id": "shared-6"}]},
        )
        catalog = asyncio.run(service.union(table=table_of()))
        vendor_row = next(
            e for e in catalog.entries if e.id.startswith("claude-acme/")
        )
        self.assertEqual(vendor_row.window_source, cat.WINDOW_UNKNOWN)
        self.assertNotIn(ONE_M_SUFFIX, vendor_row.id)

    def test_an_inherited_window_is_never_inherited_from_again(self) -> None:
        """Otherwise one unverified figure walks a whole family and reads,
        at the far end, exactly like a fact."""
        rows = [
            entry("acme-5", window=cat.ONE_M_WINDOW),
            entry("acme-6"),
            entry("acme-7"),
        ]
        resolved = resolve_all(rows, table_of())
        # Both inherit — but both from acme-5, the only VERIFIED row.
        self.assertEqual(
            resolved["acme-7"].source, f"{cat.WINDOW_INHERITED_PREFIX}acme-5",
        )

    def test_the_largest_older_window_wins(self) -> None:
        rows = [
            entry("acme-4", window=cat.ONE_M_WINDOW),
            entry("acme-5", window=200_000),
            entry("acme-6"),
        ]
        self.assertEqual(
            resolve_all(rows, table_of())["acme-6"].window, cat.ONE_M_WINDOW,
        )

    def test_a_tombstoned_row_is_not_rescued_by_a_floor(self) -> None:
        rows = [entry("acme-5", window=cat.ONE_M_WINDOW), entry("acme-6")]
        resolved = resolve_all(rows, table_of(tombstones=("acme-6",)))
        self.assertEqual(resolved["acme-6"].source, cat.WINDOW_DELETED)

    def test_inheritance_is_logged_once_per_process_not_per_request(self) -> None:
        """A picker refresh every few hours must not reprint the sentence."""
        cat._INHERITED_LOGGED.clear()
        self.addCleanup(cat._INHERITED_LOGGED.clear)
        rows = [entry("acme-91", window=cat.ONE_M_WINDOW), entry("acme-92")]
        with self.assertLogs(cat.logger, level=logging.INFO) as captured:
            resolve_all(rows, table_of())
        self.assertEqual(len(captured.output), 1)
        self.assertIn("acme-91", captured.output[0])
        with self.assertNoLogs(cat.logger, level=logging.INFO):
            resolve_all(rows, table_of())


class OneMFollowsTheWindowTests(unittest.IsolatedAsyncioTestCase):
    """The advert is derived, so a new 1M model needs no table edit."""

    async def test_an_upstream_1m_model_gets_its_companion_with_no_table_row(self) -> None:
        service = _service_with(
            first_payload={
                "data": [
                    {"id": "claude-newmodel-9", "max_input_tokens": cat.ONE_M_WINDOW},
                ]
            },
        )
        ids = [e.id for e in (await service.union(table=table_of())).entries]
        self.assertIn("claude-newmodel-9", ids)
        self.assertIn(f"claude-newmodel-9{ONE_M_SUFFIX}", ids)

    async def test_an_upstream_200k_model_gets_no_companion(self) -> None:
        """The leave-alone half of the same gate."""
        service = _service_with(
            first_payload={
                "data": [{"id": "claude-newmodel-9", "max_input_tokens": 200_000}],
            }
        )
        ids = [e.id for e in (await service.union(table=table_of())).entries]
        self.assertEqual(ids, ["claude-newmodel-9"])

    async def test_a_table_row_still_overrides_an_upstream_claim(self) -> None:
        """The vendor-window rule: a cited row wins over what upstream says."""
        service = _service_with(
            first_payload={
                "data": [{"id": "claude-newmodel-9", "max_input_tokens": cat.ONE_M_WINDOW}],
            }
        )
        catalog = await service.union(table=table_of({"claude-newmodel-9": 200_000}))
        self.assertEqual([e.id for e in catalog.entries], ["claude-newmodel-9"])

    async def test_an_inherited_1m_window_advertises_too(self) -> None:
        """End to end, this is the owner's requirement (b): the successor is
        usable at the full window on the day it appears."""
        service = _service_with(
            first_payload={
                "data": [
                    {"id": "claude-acme-5", "max_input_tokens": cat.ONE_M_WINDOW},
                    {"id": "claude-acme-6"},
                ]
            }
        )
        catalog = await service.union(
            table=table_of(), catalog_filter=cat.CATALOG_FILTER_ALL,
        )
        ids = [e.id for e in catalog.entries]
        self.assertIn(f"claude-acme-6{ONE_M_SUFFIX}", ids)


class TableDisagreementWarningTests(unittest.TestCase):
    """``window_1m`` and ``context_window`` are two hand-edited fields."""

    def _table(self, *, window: int, flag: bool) -> ContextTable:
        return ContextTable(
            rows={
                "acme-5": ModelContext(
                    model_id="acme-5", vendor="acme", context_window=window,
                    max_output=1_000, window_1m=flag,
                    source="https://docs.acme.example/5",
                )
            },
            source="test", path=None,
        )

    def setUp(self) -> None:
        cat._WINDOW_DISAGREEMENT_LOGGED.clear()
        self.addCleanup(cat._WINDOW_DISAGREEMENT_LOGGED.clear)

    def test_a_contradicting_row_is_reported_not_resolved_silently(self) -> None:
        with self.assertLogs(cat.logger, level=logging.WARNING) as captured:
            published, _ = cat._publish_family(
                entries=[entry("acme-5")],
                table=self._table(window=200_000, flag=True),
                label="Acme",
                vendor=None,
                catalog_filter=cat.CATALOG_FILTER_LATEST,
            )
        self.assertIn("acme-5", "\n".join(captured.output))
        # ...and the advert follows the WINDOW, the number that is cited.
        self.assertEqual([e.id for e in published], ["acme-5"])

    def test_an_agreeing_row_says_nothing(self) -> None:
        with self.assertNoLogs(cat.logger, level=logging.WARNING):
            cat._publish_family(
                entries=[entry("acme-5")],
                table=self._table(window=cat.ONE_M_WINDOW, flag=True),
                label="Acme",
                vendor=None,
                catalog_filter=cat.CATALOG_FILTER_LATEST,
            )


class LatestOnlyFilterTests(unittest.IsolatedAsyncioTestCase):
    """Owner requirement (a), and the groupings it depends on."""

    SHIPPED_FIRST_PARTY = {
        "data": [
            {"id": "claude-fable-5-1"}, {"id": "claude-fable-5"},
            {"id": "claude-opus-5"}, {"id": "claude-opus-4-8"},
            {"id": "claude-haiku-4-5-20251001"},
        ]
    }
    SHIPPED_VENDOR = {
        "data": [
            {"id": "glm-5.3"}, {"id": "glm-5.3-flash"}, {"id": "glm-5.2"},
            {"id": "glm-5.1"}, {"id": "glm-5"}, {"id": "glm-5-turbo"},
            {"id": "glm-4.5-air"},
        ]
    }

    async def _catalog(self, **over) -> cat.CatalogUnion:
        service = _service_with(
            first_payload=self.SHIPPED_FIRST_PARTY,
            vendor_payload=self.SHIPPED_VENDOR,
        )
        return await service.union(table=table_of(), **over)

    async def test_the_newest_version_of_each_family_survives(self) -> None:
        ids = [e.id for e in (await self._catalog()).entries]
        self.assertIn("claude-fable-5-1", ids)
        self.assertNotIn("claude-fable-5", ids)
        self.assertIn("claude-opus-5", ids)
        self.assertNotIn("claude-opus-4-8", ids)

    async def test_variant_lines_are_families_of_their_own(self) -> None:
        """The grouping mistake that would quietly delete a model: ``flash``,
        ``turbo`` and ``air`` each stand alone, at whatever version."""
        ids = [e.id for e in (await self._catalog()).entries]
        for survivor in (
            "claude-acme/glm-5.3", "claude-acme/glm-5.3-flash",
            "claude-acme/glm-5-turbo", "claude-acme/glm-4.5-air",
        ):
            self.assertIn(survivor, ids)
        for hidden in ("claude-acme/glm-5.2", "claude-acme/glm-5.1", "claude-acme/glm-5"):
            self.assertNotIn(hidden, ids)

    async def test_withheld_ids_are_reported_not_merely_absent(self) -> None:
        catalog = await self._catalog()
        self.assertIn("claude-fable-5", catalog.hidden)
        self.assertIn("claude-acme/glm-5.2", catalog.hidden)
        # The hidden list is in the vocabulary of the picker, not the vendor's.
        self.assertNotIn("glm-5.2", catalog.hidden)

    async def test_the_knob_publishes_everything(self) -> None:
        catalog = await self._catalog(catalog_filter=cat.CATALOG_FILTER_ALL)
        ids = [e.id for e in catalog.entries]
        self.assertIn("claude-fable-5", ids)
        self.assertIn("claude-acme/glm-5.1", ids)
        self.assertEqual(catalog.hidden, [], "all withholds nothing")

    async def test_a_dated_id_orders_by_generation_not_by_its_date(self) -> None:
        """``…-4-5-20251001`` must not outrank a ``5`` in the same family."""
        service = _service_with(
            first_payload={
                "data": [
                    {"id": "claude-haiku-4-5-20251001"}, {"id": "claude-haiku-5"},
                ]
            }
        )
        ids = [e.id for e in (await service.union(table=table_of())).entries]
        self.assertEqual(ids, ["claude-haiku-5"])

    async def test_created_at_breaks_a_tie_the_version_cannot(self) -> None:
        service = _service_with(
            first_payload={
                "data": [
                    {"id": "claude-twin-5", "created_at": "2026-01-01T00:00:00Z"},
                    {"id": "claude-twin-5", "created_at": "2026-06-01T00:00:00Z"},
                ]
            }
        )
        catalog = await service.union(table=table_of())
        self.assertEqual(len(catalog.entries), 1)
        self.assertEqual(catalog.entries[0].created_at, "2026-06-01T00:00:00Z")

    async def test_a_companion_never_outlives_the_row_it_belongs_to(self) -> None:
        """A ``[1m]`` row for a hidden model would be selectable and orphaned."""
        service = _service_with(
            first_payload={
                "data": [
                    {"id": "claude-fable-5", "max_input_tokens": cat.ONE_M_WINDOW},
                    {"id": "claude-fable-5-1", "max_input_tokens": cat.ONE_M_WINDOW},
                ]
            }
        )
        ids = [e.id for e in (await service.union(table=table_of())).entries]
        self.assertEqual(
            ids, ["claude-fable-5-1", f"claude-fable-5-1{ONE_M_SUFFIX}"],
        )


class DescriptionTests(unittest.TestCase):
    """The one row field the picker shows besides the name."""

    def test_the_three_short_forms(self) -> None:
        self.assertEqual(cat.short_tokens(1_000_000), "1M")
        self.assertEqual(cat.short_tokens(200_000), "200K")
        self.assertEqual(cat.short_tokens(128_000), "128K")

    def test_an_unstated_window_reads_as_unverified_not_as_a_number(self) -> None:
        self.assertEqual(cat.short_tokens(None), cat.UNVERIFIED_WINDOW)
        self.assertEqual(cat.short_tokens(0), cat.UNVERIFIED_WINDOW)

    def test_a_matching_window_carries_no_qualifier(self) -> None:
        """Saying something when there is nothing to say turns the useful
        case into noise."""
        self.assertEqual(
            cat.describe_row(
                label="Acme", window=200_000, max_output=64_000, one_m_row=False,
            ),
            "Acme · 200K context · 64K output",
        )

    def test_a_1m_row_at_1m_carries_no_qualifier_either(self) -> None:
        self.assertEqual(
            cat.describe_row(
                label="Acme",
                window=cat.ONE_M_WINDOW,
                max_output=128_000,
                one_m_row=True,
            ),
            "Acme · 1M context · 128K output",
        )

    def test_a_small_window_warns_that_compaction_fires_late(self) -> None:
        text = cat.describe_row(
            label="Acme", window=128_000, max_output=96_000, one_m_row=False,
        )
        self.assertTrue(
            text.endswith(" · client budgets 200K — compaction fires late"), text,
        )

    def test_a_1m_models_plain_row_points_at_its_companion(self) -> None:
        text = cat.describe_row(
            label="Acme",
            window=cat.ONE_M_WINDOW,
            max_output=128_000,
            one_m_row=False,
        )
        self.assertTrue(
            text.endswith(
                " · 200K budget on this row; pick the (1M context) row "
                "for the full window"
            ),
            text,
        )
        # The phrase names the row that actually exists in the picker.
        self.assertIn(cat.ONE_M_DISPLAY_SUFFIX.strip(), text)

    def test_an_unverified_window_carries_no_qualifier(self) -> None:
        """Nothing is known, so nothing can be claimed about the mismatch."""
        self.assertEqual(
            cat.describe_row(
                label="Acme", window=None, max_output=None, one_m_row=False,
            ),
            f"Acme · {cat.UNVERIFIED_WINDOW} context · "
            f"{cat.UNVERIFIED_WINDOW} output",
        )

    def test_a_vendor_row_is_labelled_with_the_vendors_own_name(self) -> None:
        from model_router.vendors import vendor_display_name

        service = _service_with(vendor_payload={"data": [{"id": "acme-5"}]})
        catalog = asyncio.run(service.union(table=table_of()))
        row = next(e for e in catalog.entries if e.id.startswith("claude-acme/"))
        self.assertTrue(
            row.description.startswith(vendor_display_name(ACME)), row.description,
        )

    def test_a_first_party_row_is_labelled_for_the_first_party(self) -> None:
        service = _service_with(first_payload={"data": [{"id": "claude-x"}]})
        catalog = asyncio.run(service.union(table=table_of()))
        row = next(e for e in catalog.entries if e.id == "claude-x")
        self.assertTrue(row.description.startswith("Anthropic"), row.description)


class ResponseFieldTests(unittest.TestCase):
    """What reaches the wire: three read fields, four relayed, two extensions."""

    def test_the_fields_the_client_reads_are_always_present(self) -> None:
        row = cat.to_models_response(
            [cat.CatalogEntry("a", "A", description="D")], {},
        )["data"][0]
        for key in ("type", "id", "display_name", "description"):
            self.assertIn(key, row)

    def test_upstream_fields_are_relayed_when_upstream_stated_them(self) -> None:
        row = cat.to_models_response(
            [
                cat.CatalogEntry(
                    "a", "A",
                    max_input_tokens=cat.ONE_M_WINDOW,
                    max_tokens=64_000,
                    created_at="2026-01-01T00:00:00Z",
                    capabilities={"vision": True},
                )
            ],
            {},
        )["data"][0]
        self.assertEqual(row["max_input_tokens"], cat.ONE_M_WINDOW)
        self.assertEqual(row["max_tokens"], 64_000)
        self.assertEqual(row["created_at"], "2026-01-01T00:00:00Z")
        self.assertEqual(row["capabilities"], {"vision": True})

    def test_an_unstated_field_is_omitted_and_never_published_as_null(self) -> None:
        """So a consumer that checks for the key can tell "upstream said
        nothing" from "upstream said none"."""
        row = cat.to_models_response([cat.CatalogEntry("a", "A")], {})["data"][0]
        for key in ("max_input_tokens", "max_tokens", "created_at", "capabilities"):
            self.assertNotIn(key, row)

    def test_each_row_says_which_step_decided_its_window(self) -> None:
        row = cat.to_models_response(
            [cat.CatalogEntry("a", "A", window_source=cat.WINDOW_TABLE)], {},
        )["data"][0]
        self.assertEqual(row["_vct_window_source"], cat.WINDOW_TABLE)

    def test_the_hidden_list_is_published_sorted(self) -> None:
        body = cat.to_models_response([], {}, ["z-9", "a-1"])
        self.assertEqual(body["_vct_catalog_hidden"], ["a-1", "z-9"])

    def test_an_unfiltered_catalog_publishes_an_empty_hidden_list(self) -> None:
        """Present and empty, not absent: a consumer must not have to tell
        "nothing hidden" from "this gateway is too old to say"."""
        self.assertEqual(cat.to_models_response([], {})["_vct_catalog_hidden"], [])


class CatalogFilterKnobTests(unittest.TestCase):
    """``VCT_MODEL_GATEWAY_CATALOG`` — read exactly once, validated loudly."""

    def test_unset_is_latest(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VCT_MODEL_GATEWAY_CATALOG", None)
            self.assertEqual(cfg.resolve_catalog_filter(), cfg.CATALOG_FILTER_LATEST)

    def test_each_accepted_value_resolves_to_itself(self) -> None:
        for value in cfg.CATALOG_FILTERS:
            with self.subTest(value=value):
                with mock.patch.dict(
                    os.environ, {"VCT_MODEL_GATEWAY_CATALOG": value},
                ):
                    self.assertEqual(cfg.resolve_catalog_filter(), value)

    def test_case_and_padding_are_forgiven(self) -> None:
        with mock.patch.dict(os.environ, {"VCT_MODEL_GATEWAY_CATALOG": "  ALL "}):
            self.assertEqual(cfg.resolve_catalog_filter(), cfg.CATALOG_FILTER_ALL)

    def test_a_typo_is_refused_and_the_error_names_the_real_values(self) -> None:
        """A knob whose typo silently does nothing is the failure this
        project has paid for before."""
        with mock.patch.dict(os.environ, {"VCT_MODEL_GATEWAY_CATALOG": "newest"}):
            with self.assertRaises(cfg.CatalogFilterError) as raised:
                cfg.resolve_catalog_filter()
        for value in cfg.CATALOG_FILTERS:
            self.assertIn(value, str(raised.exception))

    def test_the_config_carries_the_resolved_value(self) -> None:
        with mock.patch.dict(os.environ, {"VCT_MODEL_GATEWAY_CATALOG": "all"}):
            self.assertEqual(
                cfg.GatewayConfig.from_env().catalog_filter, cfg.CATALOG_FILTER_ALL,
            )

    def test_the_default_config_filters_to_latest(self) -> None:
        self.assertEqual(
            cfg.GatewayConfig().catalog_filter, cfg.CATALOG_FILTER_LATEST,
        )


class ShippedDataInvariantTests(unittest.TestCase):
    """The shipped snapshot and the shipped seed, held to their own claims."""

    def setUp(self) -> None:
        async def fetch(_url, _headers):
            return None

        async def vendor_key(_vendor):
            return "key"

        self.service = cat.CatalogService(
            vendors=dict(VENDORS),
            anthropic=ANTHROPIC_FAMILY,
            fetch_json=fetch,
            oauth_token=lambda: "tok",
            vendor_key=vendor_key,
            live_ttl_s=3600,
            static_ttl_s=60,
            clock=lambda: 100.0,
        )
        self.catalog = asyncio.run(
            self.service.union(
                table=load_seed(), catalog_filter=cat.CATALOG_FILTER_ALL,
            )
        )

    def test_every_advertised_1m_row_really_resolves_to_1m(self) -> None:
        """A suffix on a row that is not 1M would make the client budget five
        times the context the model has — the inverse of the defect the
        suffix exists to fix, and the more dangerous direction."""
        for row in self.catalog.entries:
            if row.id.endswith(ONE_M_SUFFIX):
                with self.subTest(model=row.id):
                    self.assertIn("1M context", row.description)

    def test_every_1m_model_in_the_shipped_data_is_advertised_as_one(self) -> None:
        """The other direction: a 1M model with no ``[1m]`` row anywhere is a
        window the user paid for and cannot reach."""
        advertised = {row.id for row in self.catalog.entries}
        for row in self.catalog.entries:
            if "1M context" not in row.description:
                continue
            with self.subTest(model=row.id):
                self.assertTrue(
                    row.id.endswith(ONE_M_SUFFIX)
                    or f"{row.id}{ONE_M_SUFFIX}" in advertised,
                    f"{row.id} resolves to 1M but no [1m] row exists for it",
                )

    def test_the_shipped_rows_the_seed_does_not_cite_read_unverified(self) -> None:
        """Named rather than tolerated silently.

        Three snapshot ids are older first-party models the seed deliberately
        does not carry a row for (it covers the Claude 5 family). They have no
        upstream figure either — a snapshot is a list of ids — so they resolve
        to ``unknown`` and read ``unverified`` in the picker. That is the
        correct outcome, not a gap to paper over: the gateway has nothing
        cited to say about their windows.

        And nothing in the shipped data INHERITS: each of the three is older
        than its cited sibling, and inheritance only runs forwards.
        """
        by_source: dict[str, list[str]] = {}
        for row in self.catalog.entries:
            by_source.setdefault(row.window_source, []).append(row.id)
        self.assertEqual(
            sorted(by_source.get(cat.WINDOW_UNKNOWN, [])),
            ["claude-haiku-4-5-20251001", "claude-opus-4-8", "claude-sonnet-4-6"],
        )
        self.assertNotIn(cat.WINDOW_UPSTREAM, by_source, "a snapshot states none")
        self.assertEqual(
            [s for s in by_source if s.startswith(cat.WINDOW_INHERITED_PREFIX)],
            [],
            "an older model must not inherit from its successor",
        )
        for model_id in by_source[cat.WINDOW_UNKNOWN]:
            row = next(e for e in self.catalog.entries if e.id == model_id)
            with self.subTest(model=model_id):
                self.assertIn(cat.UNVERIFIED_WINDOW, row.description)

    def test_the_latest_only_picker_is_the_list_a_user_would_choose_from(self) -> None:
        """The shipped result of requirement (a), spelled out so a regression
        in the grouping is a named diff rather than a count."""
        latest = asyncio.run(self.service.union(table=load_seed()))
        self.assertEqual(
            [row.id for row in latest.entries],
            [
                "claude-fable-5-1", f"claude-fable-5-1{ONE_M_SUFFIX}",
                "claude-haiku-4-5-20251001",
                "claude-opus-5", f"claude-opus-5{ONE_M_SUFFIX}",
                "claude-sonnet-5", f"claude-sonnet-5{ONE_M_SUFFIX}",
                f"claude-gw/glm-5.3{ONE_M_SUFFIX}",
                f"claude-gw/glm-5.3-flash{ONE_M_SUFFIX}",
                "claude-gw/glm-5-turbo",
                "claude-gw/glm-4.5-air",
            ],
        )
        self.assertEqual(
            sorted(latest.hidden),
            [
                "claude-fable-5",
                "claude-gw/glm-4.5", "claude-gw/glm-4.6", "claude-gw/glm-4.7",
                "claude-gw/glm-5", "claude-gw/glm-5.1",
                f"claude-gw/glm-5.2{ONE_M_SUFFIX}",
                "claude-opus-4-8", "claude-sonnet-4-6",
            ],
        )


class GatewaySurfaceTests(GatewayTestBase):
    """The two handlers, over HTTP, against the stub upstreams."""

    async def test_health_reports_the_filter_and_what_it_withheld(self) -> None:
        self.anthropic_up.models_payload = {
            "data": [{"id": "claude-fable-5-1"}, {"id": "claude-fable-5"}],
        }
        self.vendor_up.models_payload = {"data": []}

        before = await (await self.client.get("/health")).json()
        self.assertEqual(before["catalog_filter"], cat.CATALOG_FILTER_LATEST)
        self.assertEqual(
            before["catalog_hidden"], 0, "nothing is hidden before a catalog exists",
        )

        await self.client.get("/v1/models", headers=self.auth())
        after = await (await self.client.get("/health")).json()
        self.assertGreaterEqual(after["catalog_hidden"], 1)

    async def test_models_publishes_the_hidden_ids(self) -> None:
        self.anthropic_up.models_payload = {
            "data": [{"id": "claude-fable-5-1"}, {"id": "claude-fable-5"}],
        }
        self.vendor_up.models_payload = {"data": []}
        body = await (await self.client.get("/v1/models", headers=self.auth())).json()
        self.assertIn("claude-fable-5", body["_vct_catalog_hidden"])
        self.assertNotIn(
            "claude-fable-5", [row["id"] for row in body["data"]],
        )

    async def test_the_knob_changes_what_the_picker_shows(self) -> None:
        """The observable-difference test: same fetches, one field changed."""
        self.anthropic_up.models_payload = {
            "data": [{"id": "claude-fable-5-1"}, {"id": "claude-fable-5"}],
        }
        self.vendor_up.models_payload = {"data": []}
        # The app holds this config object, and the handler reads the field
        # per request — so the knob is exercised through the real handler
        # rather than through a second construction path.
        self.config.catalog_filter = cat.CATALOG_FILTER_ALL
        body = await (await self.client.get("/v1/models", headers=self.auth())).json()
        ids = [row["id"] for row in body["data"]]
        self.assertIn("claude-fable-5", ids)
        self.assertEqual(body["_vct_catalog_hidden"], [])

    async def test_every_published_row_carries_a_description(self) -> None:
        """It is one of the three fields the client reads; a blank one is a
        row that tells the user nothing about which subscription answers."""
        self.anthropic_up.models_payload = {"data": [{"id": "claude-opus-5"}]}
        self.vendor_up.models_payload = {"data": [{"id": "glm-5.3"}]}
        body = await (await self.client.get("/v1/models", headers=self.auth())).json()
        for row in body["data"]:
            with self.subTest(model=row["id"]):
                self.assertTrue(row["description"].strip(), row)
                self.assertIn("context", row["description"])

    async def test_a_hidden_id_is_still_selectable_by_name(self) -> None:
        """The filter narrows the PICKER, never the router: a user who knows
        the id (or has it pinned in a setting) must still reach the model."""
        self.anthropic_up.models_payload = {"data": []}
        self.vendor_up.models_payload = {
            "data": [{"id": "glm-5.3"}, {"id": "glm-5.1"}],
        }
        body = await (await self.client.get("/v1/models", headers=self.auth())).json()
        self.assertIn("claude-gw/glm-5.1", body["_vct_catalog_hidden"])
        answer = await self.client.post(
            "/v1/messages",
            headers=self.auth(),
            json={"model": "claude-gw/glm-5.1", "messages": []},
        )
        self.assertEqual(answer.status, 200)
        forwarded = json.loads(self.vendor_up.message_requests[-1]["body"])
        self.assertEqual(forwarded["model"], "glm-5.1")


def _service_with(
    *,
    first_payload: dict | None = None,
    vendor_payload: dict | None = None,
    vendor: Vendor = ACME,
    anthropic: AnthropicFamily = ACME_FAMILY,
) -> cat.CatalogService:
    """A catalog service over two synthetic upstreams, no sockets."""

    async def fetch(url, _headers):
        if "first.example" in url:
            return first_payload
        return vendor_payload

    async def vendor_key(_vendor):
        return "key"

    return cat.CatalogService(
        vendors={vendor.vendor_id: vendor},
        anthropic=anthropic,
        fetch_json=fetch,
        oauth_token=lambda: "tok",
        vendor_key=vendor_key,
        live_ttl_s=3600,
        static_ttl_s=60,
        clock=_Clock(),
    )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
