# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.95 Q4 — a brand-new model gets the right window before anyone edits a table.

USER RULING 2026-09-17, verbatim: "not all models will be 1m, IIRC I suggested
that new model versions inherit the highest context in the family (i.e. any new
Sonnet model has 1m even if old sonnet used to have 256k), if model from an
unknown family I'd assume 256k context until we manually research the true size
and add it to the table we have".

Why it matters at all: Claude Code sizes its context bar and its ``/compact``
threshold from the model ID (``[1m]`` => 1M, otherwise a 200K budget behind a
gateway — R3-CORRECTED). So the decoration a settings value carries IS the
window the client budgets, and a 1M model that ships tomorrow reads as 200K —
auto-compacting at a fifth of its real capacity — until someone edits a file.

The resolution has ONE home, :meth:`ContextTable.assume_window`, which reads
the existing ``chat_model_context.json`` and nothing else. No second rule
table, and no network: a settings write that hung on a dead gateway daemon
would be a worse bug than the lag it fixes.

Tests here are DECISIONS, both ways: what inherits, what does NOT inherit from
a neighbouring family, what a named row keeps, and what an unknown family
assumes. The end-to-end leg drives the real writer against the SHIPPED seed,
because the seed is what a user actually has.
"""
from __future__ import annotations

import json
import urllib.request
from pathlib import Path

import pytest

from model_router import context_table as ct
from model_router.context_table import ContextTable, ModelContext
from vco_lib import vscode_settings as vs

TOKEN = "q4-synthetic-host-token-not-a-real-credential"
BASE_URL = "http://127.0.0.1:11436"


def _table(rows: dict[str, tuple[str, int]], *, tombstones=()) -> ContextTable:
    """A table from ``{model_id: (vendor, window)}``. Cited, so nothing is dropped."""
    return ContextTable(
        rows={
            model_id: ModelContext(
                model_id=model_id,
                vendor=vendor,
                context_window=window,
                max_output=64_000,
                window_1m=window >= ct.ONE_M_WINDOW,
                source="https://example.invalid/cited",
            )
            for model_id, (vendor, window) in rows.items()
        },
        source=ct.SOURCE_EXPORT,
        path=None,
        tombstones=frozenset(tombstones),
    )


# ---------------------------------------------------------------------------
# 1. The rule itself
# ---------------------------------------------------------------------------


def test_a_new_version_inherits_the_highest_window_in_its_family():
    """"any new Sonnet model has 1m" — the auto-update half of the ruling."""
    table = _table({
        "claude-sonnet-4-5": ("anthropic", 200_000),
        "claude-sonnet-5": ("anthropic", ct.ONE_M_WINDOW),
    })
    verdict = table.assume_window("claude-sonnet-6")
    assert verdict.window == ct.ONE_M_WINDOW
    assert verdict.source == ct.ASSUMED_FROM_FAMILY
    assert verdict.inherited_from == "claude-sonnet-5"
    assert table.advertise_1m("claude-sonnet-6") is True


def test_a_family_inherits_its_own_highest_and_never_a_neighbours():
    """A new Haiku takes Haiku's number, not the Sonnet sitting beside it.

    This is the failure the family stem exists to prevent: one family's jump
    to 1M must not hand every other family a 5x overstatement, which the
    client would act on by not compacting until the vendor refused the turn.
    """
    # ``claude-sonnet-4-1`` is deliberately OLDER than the queried
    # ``claude-haiku-5`` by version tuple ((4, 1) < (5,)): without the family
    # check it would be an eligible sibling and would win on window size, so
    # this table discriminates the family rule rather than the version one.
    table = _table({
        "claude-haiku-4-5": ("anthropic", 200_000),
        "claude-sonnet-4-1": ("anthropic", ct.ONE_M_WINDOW),
        "claude-sonnet-5": ("anthropic", ct.ONE_M_WINDOW),
    })
    verdict = table.assume_window("claude-haiku-5")
    assert verdict.window == 200_000
    assert verdict.inherited_from == "claude-haiku-4-5"
    assert table.advertise_1m("claude-haiku-5") is False


def test_an_unknown_family_assumes_the_owners_number():
    """"if model from an unknown family I'd assume 256k context"."""
    table = _table({"claude-sonnet-5": ("anthropic", ct.ONE_M_WINDOW)})
    verdict = table.assume_window("acme-1")
    assert verdict.window == 256_000 == ct.UNKNOWN_FAMILY_WINDOW
    assert verdict.source == ct.ASSUMED_FLOOR
    assert verdict.inherited_from is None
    assert table.advertise_1m("acme-1") is False


def test_a_named_row_keeps_its_own_window_over_a_higher_family_floor():
    """"The table remains the source of truth once a model is named there."

    Without this, the ruling would be unfixable: the only way to correct a
    wrong inherited window is to add a row, and a row that lost to the family
    would correct nothing.
    """
    table = _table({
        "claude-sonnet-5": ("anthropic", ct.ONE_M_WINDOW),
        "claude-sonnet-6": ("anthropic", 200_000),
    })
    verdict = table.assume_window("claude-sonnet-6")
    assert verdict.window == 200_000
    assert verdict.source == ct.ASSUMED_FROM_TABLE
    assert table.advertise_1m("claude-sonnet-6") is False


def test_inheritance_runs_newer_from_older_and_never_backwards():
    """An OLDER unknown id does not borrow a window it never had."""
    table = _table({"claude-sonnet-5": ("anthropic", ct.ONE_M_WINDOW)})
    verdict = table.assume_window("claude-sonnet-3")
    assert verdict.source == ct.ASSUMED_FLOOR
    assert table.advertise_1m("claude-sonnet-3") is False


def test_a_tombstoned_id_does_not_inherit_its_family():
    """Deleted is deleted, family included — see :meth:`lookup`'s own argument."""
    table = _table(
        {
            "claude-sonnet-5": ("anthropic", ct.ONE_M_WINDOW),
            "claude-sonnet-6": ("anthropic", ct.ONE_M_WINDOW),
        },
        tombstones=("claude-sonnet-6",),
    )
    assert table.assume_window("claude-sonnet-6").source == ct.ASSUMED_FLOOR
    assert table.advertise_1m("claude-sonnet-6") is False


def test_a_deleted_row_is_not_a_source_of_inheritance_either():
    table = _table(
        {"claude-sonnet-5": ("anthropic", ct.ONE_M_WINDOW)},
        tombstones=("claude-sonnet-5",),
    )
    assert table.assume_window("claude-sonnet-6").source == ct.ASSUMED_FLOOR


def test_a_namespaced_id_inherits_within_its_own_vendor():
    """Two vendors, one family stem: the namespace decides which rows count.

    The other vendor's row is the one with the BIGGER window, so a resolution
    that pooled both vendors would take it and overstate a 200K model by 5x.
    (For a BARE id no namespace is available and the stem is all there is —
    the documented failure case of :meth:`assume_window`.)
    """
    table = _table({
        "glm-4": ("zai", 200_000),
        "glm-5.1": ("other-vendor", ct.ONE_M_WINDOW),
    })
    verdict = table.assume_window("claude-gw/glm-5.4")
    assert verdict.inherited_from == "glm-4"
    assert verdict.window == 200_000


def test_a_row_with_no_usable_window_is_not_inherited_from():
    """A zero is a data defect, not a window; it must not propagate."""
    table = _table({
        "claude-sonnet-5": ("anthropic", 0),
        "claude-sonnet-4-5": ("anthropic", 200_000),
    })
    assert table.assume_window("claude-sonnet-6").inherited_from == "claude-sonnet-4-5"


def test_an_empty_family_matches_nothing():
    """An id with no alphabetic segment tells us nothing about kinship."""
    table = _table({"7": ("anthropic", ct.ONE_M_WINDOW)})
    assert table.assume_window("9").source == ct.ASSUMED_FLOOR


# ---------------------------------------------------------------------------
# 2. The consumer: decorate_1m, against the SHIPPED seed
# ---------------------------------------------------------------------------


def _settings(tmp_path: Path, block: dict) -> Path:
    user = tmp_path / "Code" / "User"
    user.mkdir(parents=True, exist_ok=True)
    path = user / "settings.json"
    path.write_text(
        json.dumps({vs.ENV_BLOCK_KEY: block}, indent=4) + "\n", encoding="utf-8",
    )
    return path


def _block(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))[vs.ENV_BLOCK_KEY]


@pytest.fixture(autouse=True)
def _fresh_table_cache():
    vs._CONTEXT_TABLE_LOADER = None
    yield
    vs._CONTEXT_TABLE_LOADER = None


@pytest.fixture(autouse=True)
def _offline_gateway_probe(monkeypatch):
    monkeypatch.setattr(vs, "probe_gateway", lambda **_kw: vs.GATEWAY_STOPPED)


def test_the_shipped_seed_makes_a_brand_new_1m_model_decorated(tmp_path: Path):
    """Q4's headline: no manual edit needed for a model that ships tomorrow.

    ``claude-sonnet-6`` is in no table anywhere; ``claude-sonnet-5`` is 1M in
    the shipped seed. Before this, the panel Default carried a plain id and
    the client budgeted 200K for a 1M model.
    """
    path = _settings(tmp_path, {})
    out = vs.point_at_gateway(
        path, base_url=BASE_URL, token=TOKEN, model="claude-sonnet-6",
    )
    assert out["ok"], out["message"]
    assert _block(path)[vs.MODEL_KEY] == "claude-sonnet-6[1m]"


def test_a_model_the_seed_knows_is_not_1m_stays_undecorated(tmp_path: Path):
    """The other way, so the test cannot pass by decorating everything."""
    path = _settings(tmp_path, {"ANTHROPIC_SMALL_FAST_MODEL": "claude-gw/glm-5.1"})
    out = vs.point_at_gateway(path, base_url=BASE_URL, token=TOKEN)
    assert out["ok"]
    assert _block(path)["ANTHROPIC_SMALL_FAST_MODEL"] == "claude-gw/glm-5.1"


def test_an_unknown_family_in_a_slot_stays_undecorated(tmp_path: Path):
    path = _settings(tmp_path, {"ANTHROPIC_SMALL_FAST_MODEL": "acme-1"})
    vs.point_at_gateway(path, base_url=BASE_URL, token=TOKEN)
    assert _block(path)["ANTHROPIC_SMALL_FAST_MODEL"] == "acme-1"


def test_the_decoration_never_touches_the_network(tmp_path: Path, monkeypatch):
    """"degrade to the table when the gateway is down" — it never asks it.

    Driven rather than asserted from the source: every outbound HTTP call in
    this process is made to explode, and the write still produces the right
    id. A reader who later "improves" this by fetching ``/v1/models`` on the
    settings path turns this test red, which is the point.
    """
    def _explode(*_a, **_kw):
        raise AssertionError("the settings writer must not open a connection")

    monkeypatch.setattr(urllib.request, "urlopen", _explode)
    monkeypatch.setattr(
        "http.client.HTTPConnection.request", _explode, raising=True,
    )
    path = _settings(tmp_path, {})
    out = vs.point_at_gateway(
        path, base_url=BASE_URL, token=TOKEN, model="claude-sonnet-6",
    )
    assert out["ok"]
    assert _block(path)[vs.MODEL_KEY] == "claude-sonnet-6[1m]"


def test_a_broken_table_disables_the_decoration_rather_than_the_write(
    tmp_path: Path, monkeypatch,
):
    """Startup-path contract: no table, no decoration, still a written file."""
    monkeypatch.setattr(vs, "_context_table", lambda: None)
    path = _settings(tmp_path, {})
    out = vs.point_at_gateway(
        path, base_url=BASE_URL, token=TOKEN, model="claude-sonnet-6",
    )
    assert out["ok"]
    assert _block(path)[vs.MODEL_KEY] == "claude-sonnet-6"


# ---------------------------------------------------------------------------
# 3. One home: the catalog and the table agree about who is an older sibling
# ---------------------------------------------------------------------------


def test_both_floor_users_share_one_inheritance_predicate():
    """The catalog's floor and the table's floor ask the same function.

    Two copies of "same family, older version" would drift silently — both
    sides would still answer, just differently, about which window a new
    model inherits.
    """
    from model_router import catalog as cat
    from model_router.model_family import is_older_sibling, parse_model_id

    assert cat.is_older_sibling is is_older_sibling
    assert ct.is_older_sibling is is_older_sibling
    assert is_older_sibling(
        parse_model_id("claude-sonnet-5"), parse_model_id("claude-sonnet-6"),
    )
    assert not is_older_sibling(
        parse_model_id("claude-sonnet-6"), parse_model_id("claude-sonnet-5"),
    )
    assert not is_older_sibling(
        parse_model_id("claude-haiku-4-5"), parse_model_id("claude-sonnet-6"),
    )


def test_the_one_million_threshold_is_defined_once():
    from model_router import catalog as cat

    assert cat.ONE_M_WINDOW is ct.ONE_M_WINDOW == 1_000_000
