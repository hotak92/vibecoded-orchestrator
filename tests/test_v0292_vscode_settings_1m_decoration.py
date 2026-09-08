# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""R41 — the ``[1m]`` context-window decoration in the VS Code panel writer.

The bug this pins (observed live 2026-09-04, machine-local prototype): model
ids that arrive as LITERAL strings — a tier-slot override, a subagent slot,
an explicit ``ANTHROPIC_MODEL`` — never pass through ``/v1/models``
discovery, so the client assigns them its conservative default context
window even when the model is 1M-windowed. A session sized under a 1M
assumption then crosses the (much smaller) assumed threshold on the next
turn and auto-compacts immediately.

The fix, under ruling R41: every model-id value the writer touches — freshly
written OR carried forward by the merge — is decorated with Claude Code's
``[1m]`` client-side hint when the version-keyed chat-model context table
(``model_router.context_table``, shipped seed) marks the id 1M-windowed.

R41's boundary is asserted here, not just documented:

* PERMITTED — appending the context-window variant of the SAME model id,
  from a version-specific table, reported in the done message.
* FORBIDDEN — changing which model an id names, which includes any
  prefix/wildcard table match (``glm-5.2`` is 1M while ``glm-5.1`` is 200K;
  a wrong ``window_1m`` inverts the bug into silent truncation).

The integration tests run against the REAL table plumbing (the shipped
seed via :func:`vco_lib.vscode_settings._context_table`), so they also red
if the loader breaks. Pure-function cases use an explicit table so the
no-match arms are pinned independently of seed contents.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from vco_lib import vscode_settings as vs

TOKEN = "r41-synthetic-host-token-not-a-real-credential"
BASE_URL = "http://127.0.0.1:11436"


class _Table:
    """Minimal stand-in matching the interface ``decorate_1m`` uses."""

    def __init__(self, *ids: str) -> None:
        self._ids = set(ids)

    def advertise_1m(self, model_id: str) -> bool:
        return model_id in self._ids


@pytest.fixture(autouse=True)
def _fresh_table_cache():
    """The loader is cached at module level; give each test a fresh one.

    ``VCT_MODEL_GATEWAY_CONTEXT_TABLE`` overrides the export path only at
    loader construction, so a stale cached loader would silently ignore a
    test's monkeypatched env — a false pass of exactly the shape this
    release exists to remove.
    """
    vs._CONTEXT_TABLE_LOADER = None
    yield
    vs._CONTEXT_TABLE_LOADER = None


def _settings_file(tmp_path: Path, block: dict | None = None) -> Path:
    user = tmp_path / "Code" / "User"
    user.mkdir(parents=True, exist_ok=True)
    path = user / "settings.json"
    payload: dict = {"editor.fontSize": 13}
    if block is not None:
        payload[vs.ENV_BLOCK_KEY] = block
    path.write_text(json.dumps(payload, indent=4) + "\n", encoding="utf-8")
    return path


def _block(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))[vs.ENV_BLOCK_KEY]


# ---------------------------------------------------------------------------
# Pure function — the R41 boundary, stated as assertions
# ---------------------------------------------------------------------------


def test_decoration_preserves_vendor_model_and_version():
    table = _Table("glm-5.3-flash")
    assert (
        vs.decorate_1m("claude-gw/glm-5.3-flash", table)
        == "claude-gw/glm-5.3-flash[1m]"
    )
    assert vs.decorate_1m("glm-5.3-flash", table) == "glm-5.3-flash[1m]"


def test_decoration_never_matches_on_prefix_or_wildcard():
    """The never-wildcard rule from R41, as an executable assertion.

    ``glm-5.3`` is in this table; ids that merely START with it are not. A
    prefix match here is the forbidden direction: it would hand a 1M
    assumption to a model whose real window may be smaller — silent content
    loss instead of a premature compact.
    """
    table = _Table("glm-5.3")
    for near_miss in ("glm-5.3-turbo", "glm-5.31", "glm-5.3-flash", "glm-5"):
        assert vs.decorate_1m(near_miss, table) == near_miss


def test_non_1m_and_claude_ids_are_untouched_against_the_shipped_seed():
    seed = vs._context_table()
    assert seed is not None, "the shipped seed must always be loadable"
    # glm-5.1 is 200K in the seed — the version-key vindication row.
    assert vs.decorate_1m("claude-gw/glm-5.1", seed) == "claude-gw/glm-5.1"
    # Claude ids are deliberately absent from the table: the client knows
    # their windows natively, and decorating one would be a substitution.
    assert vs.decorate_1m("claude-sonnet-4-5", seed) == "claude-sonnet-4-5"
    assert vs.decorate_1m("claude-gw/claude-opus-4-5", seed) == "claude-gw/claude-opus-4-5"
    # Unknown vendors keep the conservative default (honest absence).
    assert vs.decorate_1m("kimi-k2-6", seed) == "kimi-k2-6"
    # The true arm, against the same real table.
    assert vs.decorate_1m("claude-gw/glm-5.2", seed) == "claude-gw/glm-5.2[1m]"


def test_already_suffixed_ids_are_idempotent_and_never_stripped():
    seed = vs._context_table()
    assert vs.decorate_1m("claude-gw/glm-5.3[1m]", seed) == "claude-gw/glm-5.3[1m]"
    # A suffix the table does not know is NOT removed — stripping would be
    # a modification in the opposite direction, equally unreported.
    empty = _Table()
    assert vs.decorate_1m("claude-gw/glm-5.3[1m]", empty) == "claude-gw/glm-5.3[1m]"


def test_no_table_is_identity():
    for value in ("claude-gw/glm-5.3", "glm-5.2", "anything"):
        assert vs.decorate_1m(value, None) == value


def test_non_string_and_empty_values_are_identity():
    table = _Table("glm-5.3")
    assert vs.decorate_1m("", table) == ""
    assert vs.decorate_1m(None, table) is None
    assert vs.decorate_1m(123, table) == 123


# ---------------------------------------------------------------------------
# Table loading — never break the write
# ---------------------------------------------------------------------------


def test_corrupt_export_falls_back_to_the_shipped_seed(monkeypatch, tmp_path):
    bad = tmp_path / "chat_model_context.json"
    bad.write_text("{ this is not json", encoding="utf-8")
    monkeypatch.setenv("VCT_MODEL_GATEWAY_CONTEXT_TABLE", str(bad))
    table = vs._context_table()
    assert table is not None
    assert vs.decorate_1m("claude-gw/glm-5.3", table) == "claude-gw/glm-5.3[1m]"


def test_unavailable_table_disables_decoration_but_never_breaks_the_write(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(vs, "_context_table", lambda: None)
    path = _settings_file(
        tmp_path,
        block={"ANTHROPIC_DEFAULT_HAIKU_MODEL": "claude-gw/glm-5.3-flash"},
    )
    result = vs.point_at_gateway(path, base_url=BASE_URL, token=TOKEN)
    assert result["ok"] and result["status"] == "written"
    assert (
        _block(path)["ANTHROPIC_DEFAULT_HAIKU_MODEL"]
        == "claude-gw/glm-5.3-flash"
    )
    assert result["values_healed"] == []


# ---------------------------------------------------------------------------
# point_at_gateway — decorate at every export path
# ---------------------------------------------------------------------------


def test_point_heals_stale_plain_slot_values_and_reports_them(tmp_path):
    path = _settings_file(
        tmp_path,
        block={
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": "claude-gw/glm-5.3-flash",
            "ANTHROPIC_SMALL_FAST_MODEL": "claude-gw/glm-5.3-flash",
        },
    )
    result = vs.point_at_gateway(path, base_url=BASE_URL, token=TOKEN)
    assert result["ok"] and result["status"] == "written"
    block = _block(path)
    assert block["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "claude-gw/glm-5.3-flash[1m]"
    assert block["ANTHROPIC_SMALL_FAST_MODEL"] == "claude-gw/glm-5.3-flash[1m]"
    assert result["values_healed"] == [
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        "ANTHROPIC_SMALL_FAST_MODEL",
    ]
    for key in result["values_healed"]:
        assert key in result["message"], (
            "the done message must name every healed slot — a value that "
            "changed while the report says PRESERVED is the false report "
            "this fix exists to eliminate"
        )
    # Still reported as carried-forward overrides: healing a value does not
    # make the key ours.
    assert result["slot_overrides_preserved"] == [
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        "ANTHROPIC_SMALL_FAST_MODEL",
    ]


def test_healing_only_change_is_written_not_reported_unchanged(tmp_path):
    """The anti-false-report pin: routing already correct, one stale slot.

    Pre-fix, this run was ``unchanged`` (nothing VCO writes differs) while
    the user's slot value kept its wrong context window — a silent no-op
    wearing an honest label.
    """
    path = _settings_file(
        tmp_path,
        block={
            "ANTHROPIC_BASE_URL": BASE_URL,
            "ANTHROPIC_AUTH_TOKEN": TOKEN,
            "ANTHROPIC_API_KEY": "",
            "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1",
            "ANTHROPIC_MODEL": "claude-gw/glm-5.3",
        },
    )
    result = vs.point_at_gateway(path, base_url=BASE_URL, token=TOKEN)
    assert result["status"] == "written", (
        "a run that changed values on disk must not report 'unchanged'"
    )
    assert result["backup_path"], "a content-changing write keeps a backup"
    assert result["values_healed"] == [vs.MODEL_KEY]
    assert _block(path)[vs.MODEL_KEY] == "claude-gw/glm-5.3[1m]"


def test_explicit_plain_model_choice_is_decorated(tmp_path):
    """First-party id: a vendor one is refused before it can be decorated
    (USER RULING 2026-09-08 — see tests/test_vscode_settings.py)."""
    path = _settings_file(tmp_path)
    result = vs.point_at_gateway(
        path, base_url=BASE_URL, token=TOKEN, model="claude-opus-5",
    )
    assert result["values_healed"] == [vs.MODEL_KEY]
    assert _block(path)[vs.MODEL_KEY] == "claude-opus-5[1m]"


def test_non_1m_and_claude_slot_values_pass_through_untouched(tmp_path):
    path = _settings_file(
        tmp_path,
        block={
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": "claude-gw/glm-5.1",
            "CLAUDE_CODE_SUBAGENT_MODEL": "claude-sonnet-4-5",
            "HTTPS_PROXY": "http://corp-proxy:3128",
        },
    )
    result = vs.point_at_gateway(path, base_url=BASE_URL, token=TOKEN)
    block = _block(path)
    assert block["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "claude-gw/glm-5.1"
    assert block["CLAUDE_CODE_SUBAGENT_MODEL"] == "claude-sonnet-4-5"
    assert block["HTTPS_PROXY"] == "http://corp-proxy:3128"
    assert result["values_healed"] == []


def test_removed_slot_overrides_are_not_healed(tmp_path):
    """Removal and healing are alternatives, not a pipeline."""
    path = _settings_file(
        tmp_path,
        block={"ANTHROPIC_DEFAULT_HAIKU_MODEL": "claude-gw/glm-5.3-flash"},
    )
    result = vs.point_at_gateway(
        path, base_url=BASE_URL, token=TOKEN, remove_slot_overrides=True,
    )
    assert "ANTHROPIC_DEFAULT_HAIKU_MODEL" not in _block(path)
    assert result["keys_removed"] == ["ANTHROPIC_DEFAULT_HAIKU_MODEL"]
    assert result["values_healed"] == []


def test_decoration_is_idempotent_across_re_runs(tmp_path):
    path = _settings_file(
        tmp_path,
        block={"ANTHROPIC_DEFAULT_HAIKU_MODEL": "claude-gw/glm-5.3-flash"},
    )
    first = vs.point_at_gateway(path, base_url=BASE_URL, token=TOKEN)
    assert first["values_healed"] == ["ANTHROPIC_DEFAULT_HAIKU_MODEL"]
    second = vs.point_at_gateway(path, base_url=BASE_URL, token=TOKEN)
    assert second["status"] == "unchanged"
    assert second["values_healed"] == []
    assert second["backup_path"] is None
