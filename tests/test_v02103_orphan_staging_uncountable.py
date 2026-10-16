# SPDX-License-Identifier: AGPL-3.0-or-later
"""Staging crash-recovery never drops on a count it could not read (v0.2.103 R25 C).

`_recover_or_drop_orphan_staging` decides between RECOVER (drop `<name>` when it
reads as empty, copy staging back), SAFE-DROP (drop staging) and AMBIGUOUS. It
used a 0-on-failure count, so a Weaviate blip made a populated `<name>` read as
empty — and RECOVER dropped it. An unreadable count now means "may hold
objects": nothing is dropped, created or copied, and the outcome is ambiguous.
"""
from __future__ import annotations

from unittest import mock

import pytest

from vco_lib import project_init, weaviate_helpers

NAME = "Demo_KnowledgeGraph"
STAGING = NAME + project_init._STAGING_SUFFIX


def _run(counts: dict):
    calls: list[tuple] = []
    with mock.patch.object(project_init, "_fetch_schema", lambda n, **_: {"class": n}), \
         mock.patch.object(project_init, "_count_objects", lambda n, **_: counts[n]), \
         mock.patch.object(project_init, "_delete_class",
                           lambda n, **_: calls.append(("delete", n))), \
         mock.patch.object(project_init, "_create_class",
                           lambda d, **_: calls.append(("create", d["class"]))), \
         mock.patch.object(project_init, "_copy_collection_with_vectors",
                           lambda s, d, **_: calls.append(("copy", s, d)) or 0):
        outcome = project_init._recover_or_drop_orphan_staging(
            NAME, target_def={"class": NAME}, weaviate_url="http://127.0.0.1:9")
    return outcome, calls


@pytest.mark.parametrize("counts", [
    {NAME: None, STAGING: 5},     # populated <name> unreadable: RECOVER would drop it
    {NAME: 7, STAGING: None},     # staging unreadable: SAFE-DROP would drop it
    {NAME: None, STAGING: None},
])
def test_unreadable_count_touches_nothing(counts):
    outcome, calls = _run(counts)
    assert outcome == "ambiguous"
    assert calls == []


def test_readable_counts_keep_the_existing_branches():
    assert _run({NAME: 9, STAGING: 3}) == ("dropped", [("delete", STAGING)])


def test_counter_reports_none_not_zero_when_weaviate_is_unreachable():
    with mock.patch.object(weaviate_helpers, "connect_v4", side_effect=OSError("down")):
        assert weaviate_helpers.count_objects_v4_or_none(NAME) is None
        assert project_init._count_objects(NAME) is None
