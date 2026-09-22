# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The QwenCloud Token-Plan model list, as the tests expect to see it.

One home for three lists that were copied between test modules until
2026-09-22 — `test_model_router_catalog.py` and `test_model_router_server.py`
each carried a verbatim fifteen-id payload, which is the same shape
``tests/common/module_gateway.py`` was created this cycle to end.

**These lists are written out by hand on purpose.** They are the EXPECTED
side of the assertions that check the shipped row (``VENDORS["qwen"]``), so
deriving them from that row — applying its own ``catalog_exclude_prefixes``
to the payload to compute the other two — would make every one of those
assertions tautological: the row would be compared against itself and any
change to it would stay green. The pin lives in
:func:`assert_matches_shipped_row`; call it from one test rather than
re-deriving.
"""

from __future__ import annotations

#: The live ``/v1/models`` list the QwenCloud compatible-mode base returns
#: (live-verified 2026-09-22): fifteen ids, of which six never reach the
#: picker.
QWEN_LIVE_IDS = (
    "auto",
    "deepseek-v4-flash-0731", "deepseek-v4-pro", "deepseek-v4.1-flash",
    "glm-5.2", "glm-5.3",
    "qwen3.6-flash", "qwen3.7-max", "qwen3.7-plus",
    "qwen3.8-flash", "qwen3.8-max",
    "qwen-audio-3.0-realtime-plus", "qwen-audio-3.0-tts-plus",
    "wan2.7-image", "wan2.7-image-pro",
)

#: That list as the endpoint serves it.
QWEN_LIVE_MODELS = {"data": [{"id": model_id} for model_id in QWEN_LIVE_IDS]}

#: The nine chat ids the picker may publish from it — and exactly the shipped
#: row's declared fallback.
QWEN_CHAT_NINE = (
    "qwen3.8-max", "qwen3.8-flash", "qwen3.7-max", "qwen3.7-plus",
    "qwen3.6-flash", "glm-5.3", "glm-5.2", "deepseek-v4.1-flash",
    "deepseek-v4-pro",
)

#: The six ids ``catalog_exclude_prefixes`` drops: a router alias, two voice
#: modalities, two image modalities, and the dated deepseek flash snapshot
#: (curated out by owner ruling 2026-09-22 — the versioned line is the
#: published flash).
QWEN_EXCLUDED_SIX = (
    "auto", "deepseek-v4-flash-0731",
    "qwen-audio-3.0-realtime-plus", "qwen-audio-3.0-tts-plus",
    "wan2.7-image", "wan2.7-image-pro",
)


def assert_matches_shipped_row(test, vendor) -> None:
    """Tie these hand-written expectations to the row that actually ships.

    Every id is accounted for exactly once, the excluded set is what the
    row's own prefixes select, and the published nine are the row's declared
    fallback. Pass ``VENDORS["qwen"]``.
    """
    live = set(QWEN_LIVE_IDS)
    test.assertEqual(len(QWEN_LIVE_IDS), len(live), "duplicate id in the payload")
    test.assertEqual(
        live, set(QWEN_CHAT_NINE) | set(QWEN_EXCLUDED_SIX),
        "every live id is either published or excluded",
    )
    test.assertEqual(
        set(QWEN_CHAT_NINE) & set(QWEN_EXCLUDED_SIX), set(),
        "and never both",
    )
    prefixes = tuple(vendor.catalog_exclude_prefixes)
    test.assertEqual(
        {m for m in live if m.startswith(prefixes)}, set(QWEN_EXCLUDED_SIX),
        "the shipped prefixes select exactly the excluded six",
    )
    test.assertEqual(
        tuple(vendor.static_ids), QWEN_CHAT_NINE,
        "the row's declared fallback is the published nine",
    )
