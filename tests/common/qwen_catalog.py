# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The QwenCloud Token-Plan model list, as the tests expect to see it.

One home for three lists that were copied between test modules until
2026-09-22 — `test_model_router_catalog.py` and `test_model_router_server.py`
each carried a verbatim fifteen-id payload, which is the same shape
``tests/common/module_gateway.py`` was created this cycle to end.

**These lists are written out by hand on purpose.** They are the EXPECTED
side of the assertions that check the shipped row (``VENDORS["qwen"]``), so
deriving them from that row — applying its own exclusion rules to the payload
to compute the other lists — would make every one of those assertions
tautological: the row would be compared against itself and any change to it
would stay green. The pin lives in :func:`assert_matches_shipped_row`; call it
from one test rather than re-deriving.
"""

from __future__ import annotations

#: The live ``/v1/models`` list the QwenCloud compatible-mode base returns
#: (live-verified 2026-11-10; the same list with its ``created`` values is
#: ``tests/fixtures/model_router/qwen-models-live-2026-11-10.json``): twenty
#: ids, of which eleven never reach the picker (ten excluded, one retired).
QWEN_LIVE_IDS = (
    "auto",
    "deepseek-v4-flash-0731", "deepseek-v4-pro", "deepseek-v4-pro-0813",
    "deepseek-v4.1-flash", "deepseek-v4.1-flash-1017",
    "glm-5.2", "glm-5.3",
    "qwen3.6-flash", "qwen3.7-max", "qwen3.7-plus",
    "qwen3.8-flash", "qwen3.8-max",
    "qwen-audio-3.0-asr-flash", "qwen-audio-3.0-realtime-plus",
    "qwen-audio-3.0-tts-plus", "qwen-image-3.0-pro",
    "wan2.7-image", "wan2.7-image-pro", "wan3.0-video",
)

#: That list as the endpoint serves it.
QWEN_LIVE_MODELS = {"data": [{"id": model_id} for model_id in QWEN_LIVE_IDS]}

#: The nine chat ids the picker may publish from it — and exactly the shipped
#: row's declared fallback. ``deepseek-v4.1-flash-1017`` is the one dated
#: build the row offers (owner 2026-11-10).
QWEN_CHAT_IDS = (
    "qwen3.8-max", "qwen3.8-flash", "qwen3.7-max", "qwen3.7-plus",
    "qwen3.6-flash", "glm-5.3", "deepseek-v4.1-flash-1017",
    "deepseek-v4.1-flash", "deepseek-v4-pro",
)

#: The curated-hidden three among them (``catalog_hide_ids``): reported
#: hidden, routable by name, never published.
QWEN_HIDDEN_IDS = ("qwen3.7-plus", "deepseek-v4-pro", "deepseek-v4.1-flash")

#: Live ids the endpoint still carries that the gateway does NOT provide at
#: all (owner ruling 2026-10-01): the row's ``retired_ids``. Neither published
#: nor reported hidden, and refused by name.
QWEN_RETIRED = ("glm-5.2",)

#: The ten ids the catalog drops before anything sees them: the router alias
#: and the ``wan`` family (``catalog_exclude_prefixes``), the non-chat
#: modalities (image / video / audio / asr / tts — the modality rule), and
#: the two dated builds the row does not offer (the dated-build rule, owner
#: ruling 2026-09-22).
QWEN_EXCLUDED = (
    "auto", "wan2.7-image", "wan2.7-image-pro", "wan3.0-video",
    "qwen-audio-3.0-asr-flash", "qwen-audio-3.0-realtime-plus",
    "qwen-audio-3.0-tts-plus", "qwen-image-3.0-pro",
    "deepseek-v4-flash-0731", "deepseek-v4-pro-0813",
)


def assert_matches_shipped_row(test, vendor) -> None:
    """Tie these hand-written expectations to the row that actually ships.

    Every id is accounted for exactly once, the excluded set is what the
    catalog's exclusion (prefixes + rules) selects for this row, and the
    published nine are the row's declared fallback. Pass ``VENDORS["qwen"]``.
    """
    from model_router import catalog as cat

    live = set(QWEN_LIVE_IDS)
    test.assertEqual(len(QWEN_LIVE_IDS), len(live), "duplicate id in the payload")
    test.assertEqual(
        live, set(QWEN_CHAT_IDS) | set(QWEN_EXCLUDED) | set(QWEN_RETIRED),
        "every live id is either published, excluded or retired",
    )
    test.assertEqual(
        (set(QWEN_CHAT_IDS) & set(QWEN_EXCLUDED))
        | (set(QWEN_CHAT_IDS) & set(QWEN_RETIRED)), set(),
        "and never both",
    )
    entries = tuple(cat.CatalogEntry(id=m, display_name=m) for m in QWEN_LIVE_IDS)
    kept = cat._exclude_by_rule(  # noqa: SLF001 - the real exclusion path
        cat._exclude_by_prefix(entries, vendor.catalog_exclude_prefixes),  # noqa: SLF001
        vendor,
    )
    test.assertEqual(
        live - {e.id for e in kept}, set(QWEN_EXCLUDED),
        "the shipped prefixes and rules select exactly the excluded ten "
        "(glm-5.2 survives them; the retired filter drops it after)",
    )
    test.assertEqual(
        tuple(vendor.retired_ids), QWEN_RETIRED,
        "the row retires exactly glm-5.2",
    )
    test.assertEqual(
        tuple(vendor.static_ids), QWEN_CHAT_IDS,
        "the row's declared fallback is the published nine",
    )
    test.assertEqual(
        tuple(vendor.catalog_hide_ids), QWEN_HIDDEN_IDS,
        "the row curates exactly three ids out of the picker",
    )
