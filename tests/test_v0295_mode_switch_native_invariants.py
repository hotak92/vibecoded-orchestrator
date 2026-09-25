# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.95 R4 — ``remote-control`` means the stock client, in charge of everything.

OWNER REQUIREMENT (2026-09-16), verbatim: "keep the switch between multimodel
and remotecontrol working correctly (when set to remotecontrol it just uses
Claude Code's native everything including tokens counting)".

"Native token counting" is not something VCO switches ON — it is what the
client does when nothing has configured it otherwise. The client sizes its
context window from the MODEL ID (``[1m]`` => 1M; a plain first-party id
behind a gateway is budgeted at 200K) and counts tokens itself from each
response's ``usage``. Three env knobs would take that over —
``CLAUDE_CODE_MAX_CONTEXT_TOKENS``, ``CLAUDE_CODE_DISABLE_1M_CONTEXT``,
``CLAUDE_CODE_AUTO_COMPACT_WINDOW`` — and VCO writes none of them, in either
mode. So the invariant to pin is an ABSENCE, and an absence is exactly what
rots unnoticed: nothing fails when a helper starts writing a window knob,
the accounting simply stops being the client's.

What is pinned here, all behaviourally (no source scan: a knob name in a
comment must never satisfy a check about what gets WRITTEN):

1. After the ``remote-control`` leg, no VCO routing survives — every
   ``ROUTING_KEYS`` entry and ``claudeCode.disableLoginPrompt`` are gone, so
   ``ANTHROPIC_BASE_URL`` is unset and Claude Code's endpoint gate (>= 2.1.196)
   is satisfied. Nothing gateway-only survives either.
2. A panel VCO itself pointed round-trips back to ZERO keys VCO ever writes,
   while every key that is none of VCO's survives byte-for-byte.
3. CLOSURE, the strong form of "we never write a context knob": across every
   writer in this module and a grid of starting files, the set of env-block
   keys ADDED is always a subset of ``ROUTING_KEYS | {MODEL_KEY} |
   SLOT_OVERRIDE_KEYS`` — a set that provably excludes the three knobs. A
   user's own knob rides through both legs untouched (it is their key).
4. The ``multimodel`` leg still decorates an explicit or restored FIRST-PARTY
   Default with ``[1m]`` when the table vouches for it (R41): the id IS the
   window, so this is what gives a 1M model its 1M budget.
5. ``remote-control`` twice is idempotent and loses nothing.

Not duplicated from ``tests/test_vscode_settings_mode_switch.py``: the stash
mechanics, JSONC handling, the vendor-Default rule and the exact round
trip live there and are not re-asserted here. This file is about the
NATIVE-mode guarantee, which that file never states.

Every test drives its own ``stash=`` under ``tmp_path``; the real user's
VS Code settings are never in scope (2026-09-10 lesson: a live panel session
depends on that file).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import pytest

from vco_lib import vscode_settings as vs

TOKEN = "r4-synthetic-host-token-not-a-real-credential"
BASE_URL = "http://127.0.0.1:11436"
OPUS = "claude-opus-5"
OPUS_1M = "claude-opus-5[1m]"
CLAUDE_45 = "claude-opus-4-5"
GLM_1M = "claude-gw/glm-5.3[1m]"
FLASH = "claude-gw/glm-5.3-flash"
FLASH_1M = "claude-gw/glm-5.3-flash[1m]"

#: The client-side context knobs. Setting any of these takes the window and
#: the auto-compact threshold away from the client's own model-id-derived
#: accounting — which is the thing the owner requirement protects.
CONTEXT_KNOBS = (
    "CLAUDE_CODE_MAX_CONTEXT_TOKENS",
    "CLAUDE_CODE_DISABLE_1M_CONTEXT",
    "CLAUDE_CODE_AUTO_COMPACT_WINDOW",
)

#: Every env-block key this module is allowed to put into a user's file.
#: The closure test below asserts writers never exceed it.
VCO_WRITABLE_KEYS = frozenset(vs.ROUTING_KEYS) | {vs.MODEL_KEY} | set(
    vs.SLOT_OVERRIDE_KEYS
)


class _Table:
    """The two methods the writer asks a context table for.

    Kept local (and minimal) rather than imported from a sibling test
    module: the real table is loaded from the shipped seed, and a test that
    must be deterministic about a SPECIFIC id should say so in its own file.
    """

    def __init__(self, vendors: dict[str, str], one_m: set[str] | None = None) -> None:
        self._vendors = vendors
        self._one_m = set(one_m or ())

    def lookup(self, model_id: str):
        vendor = self._vendors.get(model_id)
        if vendor is None:
            return None
        return type("Row", (), {"vendor": vendor})()

    def advertise_1m(self, model_id: str) -> bool:
        return model_id in self._one_m


@pytest.fixture()
def stash(tmp_path: Path) -> Path:
    return tmp_path / "state" / "model-gateway" / "vscode-mode-stash.json"


@pytest.fixture(autouse=True)
def _offline_gateway_probe(monkeypatch):
    """No test here may reach a real port; ``panel_mode`` probes /health."""
    monkeypatch.setattr(vs, "probe_gateway", lambda **_kw: vs.GATEWAY_STOPPED)


@pytest.fixture(autouse=True)
def _fresh_table_cache():
    """The loader is cached at module level; a stale one ignores a patch."""
    vs._CONTEXT_TABLE_LOADER = None
    yield
    vs._CONTEXT_TABLE_LOADER = None


def _settings(tmp_path: Path, payload: dict, *, name: str = "settings.json") -> Path:
    user = tmp_path / "Code" / "User"
    user.mkdir(parents=True, exist_ok=True)
    path = user / name
    path.write_text(json.dumps(payload, indent=4) + "\n", encoding="utf-8")
    return path


def _doc(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _block(path: Path) -> dict:
    if not path.is_file():
        return {}
    return _doc(path).get(vs.ENV_BLOCK_KEY, {})


def _pointed_env(extra: dict | None = None, model: str | None = None) -> dict:
    env: dict[str, Any] = {
        "ANTHROPIC_BASE_URL": BASE_URL,
        "ANTHROPIC_AUTH_TOKEN": TOKEN,
        "ANTHROPIC_API_KEY": "",
        "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1",
    }
    if model:
        env[vs.MODEL_KEY] = model
    env.update(extra or {})
    return env


# ---------------------------------------------------------------------------
# 1. The endpoint gate is satisfied: nothing VCO routed with survives
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "env"),
    [
        ("a plain pointed panel", _pointed_env()),
        ("a pointed panel with a gateway Default", _pointed_env(model=GLM_1M)),
        (
            "a pointed panel with vendor slots and user keys",
            _pointed_env(
                {
                    "ANTHROPIC_DEFAULT_HAIKU_MODEL": FLASH_1M,
                    "CLAUDE_CODE_SUBAGENT_MODEL": CLAUDE_45,
                    "HTTPS_PROXY": "http://corp-proxy:3128",
                },
                model=GLM_1M,
            ),
        ),
        (
            "a panel someone hand-edited onto a foreign gateway",
            {
                "ANTHROPIC_BASE_URL": "http://127.0.0.1:8787",
                "ANTHROPIC_AUTH_TOKEN": TOKEN,
                "ANTHROPIC_DEFAULT_HAIKU_MODEL": FLASH,
            },
        ),
    ],
)
def test_remote_control_leaves_no_vco_routing_and_nothing_gateway_only(
    tmp_path: Path, stash: Path, label: str, env: dict,
):
    """The gate is the ENDPOINT: Claude Code >= 2.1.196 refuses Remote
    Control whenever ``ANTHROPIC_BASE_URL`` is not api.anthropic.com, OAuth
    or not. So the one thing this leg must never leave behind is a routing
    key — and, for the picker to be the client's own, no id only the gateway
    could answer."""
    path = _settings(tmp_path, {"editor.fontSize": 13, vs.ENV_BLOCK_KEY: env,
                                vs.LOGIN_PROMPT_KEY: True})
    out = vs.set_mode(path, vs.MODE_REMOTE_CONTROL, stash=stash)
    assert out["ok"], (label, out)

    doc = _doc(path)
    block = doc.get(vs.ENV_BLOCK_KEY, {})
    assert not (set(block) & set(vs.ROUTING_KEYS)), f"{label}: routing survived"
    assert vs.LOGIN_PROMPT_KEY not in doc, f"{label}: login prompt still suppressed"

    table = vs._context_table()
    for key, value in block.items():
        assert not vs.is_gateway_only_model(value, table), (
            f"{label}: {key}={value!r} names a model only the gateway resolves"
        )
    assert vs.panel_mode(path)["mode"] == vs.MODE_REMOTE_CONTROL, label


def test_a_panel_vco_pointed_round_trips_to_zero_vco_keys(tmp_path: Path, stash: Path):
    """The literal form of the invariant, on the state VCO itself creates.

    ``point`` writes the four routing keys, the login-prompt key, and (only
    on an explicit first-party choice) the Default. Take that file back to
    ``remote-control`` and NOTHING this module can write is left: the panel
    is stock, byte-identical to what the user had before VCO touched it.
    """
    path = _settings(tmp_path, {"editor.fontSize": 13, "telemetry.level": "off"})
    before = _doc(path)

    pointed = vs.point_at_gateway(path, base_url=BASE_URL, token=TOKEN)
    assert pointed["ok"] and set(pointed["keys_written"]) == set(vs.ROUTING_KEYS)

    out = vs.set_mode(path, vs.MODE_REMOTE_CONTROL, stash=stash)
    assert out["ok"] and out["status"] == "written", out
    assert not (set(_block(path)) & VCO_WRITABLE_KEYS)
    assert _doc(path) == before, "the user's file came back exactly as it was"


def test_every_key_that_is_not_vcos_survives_byte_for_byte(
    tmp_path: Path, stash: Path,
):
    """The other half: the switch is not a licence to tidy a user's file."""
    mine = {
        "HTTPS_PROXY": "http://corp-proxy:3128",
        "NO_PROXY": "localhost,127.0.0.1",
        "SSL_CERT_FILE": "/etc/ssl/corp.pem",
        "EDITOR": "  spaced  value  ",
        "CLAUDE_CODE_SUBAGENT_MODEL": CLAUDE_45,
    }
    path = _settings(
        tmp_path,
        {
            "editor.fontSize": 13,
            "workbench.colorTheme": "Default Dark+",
            vs.ENV_BLOCK_KEY: _pointed_env(dict(mine), model=GLM_1M),
            vs.LOGIN_PROMPT_KEY: True,
        },
    )
    vs.set_mode(path, vs.MODE_REMOTE_CONTROL, stash=stash)

    doc = _doc(path)
    block = doc[vs.ENV_BLOCK_KEY]
    for key, value in mine.items():
        assert block[key] == value, f"{key} was not carried through verbatim"
    assert doc["editor.fontSize"] == 13
    assert doc["workbench.colorTheme"] == "Default Dark+"


def test_a_first_party_default_is_kept_and_that_is_todays_answer_to_q1(
    tmp_path: Path, stash: Path,
):
    """TODAY'S BEHAVIOUR, pinned so a change to it has to be deliberate.

    ``claude-opus-5[1m]`` resolves against api.anthropic.com, so it is not
    gateway-only and this leg — which stashes only what it drops — keeps it.
    Native everything still holds: the stock client resolves the id, sizes
    the window from it (``[1m]`` => 1M) and counts tokens itself.

    It IS still an env pin, and the documented precedence puts
    ``ANTHROPIC_MODEL`` above the ``model`` value ``/model`` saves, so the
    next launch returns to it. Whether ``point`` should write that pin at
    all is the owner's open question (plan v0.2.95 Q1). If the answer is
    "stop pinning", this test is the one that goes red — on purpose.
    """
    path = _settings(
        tmp_path,
        {vs.ENV_BLOCK_KEY: _pointed_env(model=OPUS_1M), vs.LOGIN_PROMPT_KEY: True},
    )
    out = vs.set_mode(path, vs.MODE_REMOTE_CONTROL, stash=stash)
    assert out["ok"]
    assert _block(path)[vs.MODEL_KEY] == OPUS_1M
    assert vs.MODEL_KEY not in out["values_stashed"], "kept, so nothing to stash"


# ---------------------------------------------------------------------------
# 2. Closure: the keys a writer can ADD, and why no knob is among them
# ---------------------------------------------------------------------------


def test_no_context_knob_is_in_the_set_vco_may_write():
    """The premise of the closure test, asserted rather than assumed.

    If a future change ever adds a window knob to ``ROUTING_KEYS`` (the
    plausible way it would happen — "the gateway needs the client to know
    the window"), the closure test below would still pass while the
    guarantee was gone. This is the check that catches that instead.
    """
    for knob in CONTEXT_KNOBS:
        assert knob not in VCO_WRITABLE_KEYS


def _writers(stash: Path) -> list[tuple[str, Callable[[Path], dict]]]:
    """Every entry point in this module that can write the env block."""
    return [
        ("point", lambda p: vs.point_at_gateway(p, base_url=BASE_URL, token=TOKEN)),
        (
            "point+model",
            lambda p: vs.point_at_gateway(
                p, base_url=BASE_URL, token=TOKEN, model=OPUS,
            ),
        ),
        (
            "point+drop-slots",
            lambda p: vs.point_at_gateway(
                p, base_url=BASE_URL, token=TOKEN, remove_slot_overrides=True,
            ),
        ),
        (
            # A tampered stash is the one channel that feeds names from OUTSIDE
            # this module into the env block; the knobs are in it on purpose.
            "point+restore",
            lambda p: vs.point_at_gateway(
                p,
                base_url=BASE_URL,
                token=TOKEN,
                restore_env={
                    vs.MODEL_KEY: OPUS,
                    "ANTHROPIC_DEFAULT_HAIKU_MODEL": FLASH,
                    "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "200000",
                    "CLAUDE_CODE_DISABLE_1M_CONTEXT": "1",
                    "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "0.5",
                },
            ),
        ),
        ("mode:remote-control", lambda p: vs.set_mode_remote_control(p, stash=stash)),
        (
            "mode:multimodel",
            lambda p: vs.set_mode_multimodel(
                p, base_url=BASE_URL, token=TOKEN, stash=stash,
            ),
        ),
        ("reset_native", vs.reset_native),
        ("clear_default_model", vs.clear_default_model),
        ("reset_native_if_vco_gateway", vs.reset_native_if_vco_gateway),
    ]


def _starting_files(tmp_path: Path) -> list[tuple[str, Path]]:
    """Files a writer can meet, including ones that already hold a knob."""
    cases: list[tuple[str, Path]] = [
        ("missing", tmp_path / "gone" / "settings.json"),
        ("empty", _settings(tmp_path / "empty", {})),
        (
            "stock-with-user-keys",
            _settings(
                tmp_path / "stock",
                {"editor.fontSize": 13, vs.ENV_BLOCK_KEY: {"HTTPS_PROXY": "http://p:1"}},
            ),
        ),
        (
            "pointed-with-slots",
            _settings(
                tmp_path / "pointed",
                {
                    vs.ENV_BLOCK_KEY: _pointed_env(
                        {
                            "ANTHROPIC_DEFAULT_HAIKU_MODEL": FLASH,
                            "CLAUDE_CODE_SUBAGENT_MODEL": CLAUDE_45,
                        },
                        model=GLM_1M,
                    ),
                    vs.LOGIN_PROMPT_KEY: True,
                },
            ),
        ),
        (
            "user-set-context-knobs",
            _settings(
                tmp_path / "knobs",
                {
                    vs.ENV_BLOCK_KEY: _pointed_env(
                        {knob: "1" for knob in CONTEXT_KNOBS}, model=OPUS_1M,
                    ),
                    vs.LOGIN_PROMPT_KEY: True,
                },
            ),
        ),
    ]
    return cases


def test_no_writer_can_add_a_key_outside_the_set_vco_owns(tmp_path: Path, stash: Path):
    """CLOSURE — what every writer ADDS, across every starting file.

    Stated positively (added keys must be VCO's own) rather than negatively
    ("not these three"), because the negative form only ever catches the
    knob someone thought to list. The three knobs are excluded by the
    previous test's assertion about the set itself.
    """
    for label, write in _writers(stash):
        for state, template in _starting_files(tmp_path):
            path = tmp_path / "grid" / label.replace(":", "-") / state / "settings.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            if template.is_file():
                path.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
            before = set(_block(path))

            result = write(path)

            added = set(_block(path)) - before
            assert added <= VCO_WRITABLE_KEYS, (
                f"{label} on {state} added {sorted(added - VCO_WRITABLE_KEYS)}"
            )
            for knob in CONTEXT_KNOBS:
                assert knob not in result.get("keys_written", []), (
                    f"{label} reported writing {knob}"
                )


@pytest.mark.parametrize("mode", [vs.MODE_REMOTE_CONTROL, vs.MODE_MULTIMODEL])
def test_a_users_own_context_knob_rides_through_both_modes_untouched(
    tmp_path: Path, stash: Path, mode: str,
):
    """VCO writes no knob; it also does not DELETE one the user set.

    Both halves matter. A user who pinned ``CLAUDE_CODE_MAX_CONTEXT_TOKENS``
    made a decision about their own client, and a mode switch silently
    undoing it would be the same class of surprise as writing one.
    """
    knobs = {"CLAUDE_CODE_MAX_CONTEXT_TOKENS": "400000",
             "CLAUDE_CODE_DISABLE_1M_CONTEXT": "1",
             "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "0.8"}
    path = _settings(
        tmp_path,
        {vs.ENV_BLOCK_KEY: _pointed_env(dict(knobs), model=OPUS_1M),
         vs.LOGIN_PROMPT_KEY: True},
    )
    if mode == vs.MODE_REMOTE_CONTROL:
        out = vs.set_mode(path, mode, stash=stash)
    else:
        out = vs.set_mode(path, mode, base_url=BASE_URL, token=TOKEN, stash=stash)
    assert out["ok"], out
    block = _block(path)
    for key, value in knobs.items():
        assert block[key] == value, f"{key} was not carried through {mode}"


# ---------------------------------------------------------------------------
# 3. The multimodel leg still gives a 1M model its 1M window (R41)
# ---------------------------------------------------------------------------


def test_multimodel_decorates_an_explicit_first_party_default(
    tmp_path: Path, stash: Path, monkeypatch,
):
    """The id IS the window (R3-CORRECTED 2026-09-16): a plain first-party id
    behind a gateway is budgeted at 200K, and ``[1m]`` is what buys the 1M.

    Driven against an explicit table so the assertion is about the WRITER,
    not about which rows today's seed happens to carry; the seed arm is the
    next test.
    """
    monkeypatch.setattr(
        vs, "_context_table", lambda: _Table({OPUS: "anthropic"}, {OPUS}),
    )
    path = _settings(tmp_path, {"editor.fontSize": 13})
    out = vs.point_at_gateway(path, base_url=BASE_URL, token=TOKEN, model=OPUS)
    assert out["ok"], out
    assert _block(path)[vs.MODEL_KEY] == OPUS_1M
    assert out["values_healed"] == [vs.MODEL_KEY]


def test_multimodel_decorates_a_default_restored_from_the_stash(
    tmp_path: Path, stash: Path, monkeypatch,
):
    """The restore path decorates too — a stash written by an older VCO can
    hold a plain id, and coming back from Remote Control must not cost the
    window. Uses a table that vouches for the first-party id AND calls the
    gateway id a vendor one, so the value is stashed in the first place."""
    table = _Table({OPUS: "anthropic", "glm-5.3-flash": "zai"}, {OPUS, "glm-5.3-flash"})
    monkeypatch.setattr(vs, "_context_table", lambda: table)
    path = _settings(
        tmp_path,
        {vs.ENV_BLOCK_KEY: _pointed_env(model=OPUS), vs.LOGIN_PROMPT_KEY: True},
    )
    # Force the Default into the stash by writing it there as an older VCO
    # would have: plain, and taken out of THIS file.
    vs.set_mode(path, vs.MODE_REMOTE_CONTROL, stash=stash)
    stash.parent.mkdir(parents=True, exist_ok=True)
    stash.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "stashed_at": "2026-09-16T00:00:00Z",
                "settings_path": str(path),
                "routing_keys": list(vs.ROUTING_KEYS),
                "routing_values": {},
                "login_prompt_removed": True,
                "values": {vs.MODEL_KEY: OPUS, "ANTHROPIC_DEFAULT_HAIKU_MODEL": FLASH},
            },
        ),
        encoding="utf-8",
    )

    out = vs.set_mode(
        path, vs.MODE_MULTIMODEL, base_url=BASE_URL, token=TOKEN, stash=stash,
    )
    assert out["ok"], out
    block = _block(path)
    assert block[vs.MODEL_KEY] == OPUS_1M, "a restored Default keeps its window"
    assert block["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == FLASH_1M
    assert out["refusal_reason"] is None


def test_the_shipped_seed_vouches_for_the_first_party_default_vco_offers():
    """The seed arm: the id the GUI pre-selects is one the table knows is 1M,
    so the decoration above is not theoretical for the default choice."""
    table = vs._context_table()
    assert table is not None, "the shipped seed must always be loadable"
    assert vs.decorate_1m(vs.DEFAULT_GATEWAY_MODEL, table) == (
        vs.DEFAULT_GATEWAY_MODEL + vs.CONTEXT_1M_SUFFIX
    )


# ---------------------------------------------------------------------------
# 4. Twice is idempotent, and loses nothing
# ---------------------------------------------------------------------------


def test_remote_control_twice_is_idempotent_and_loses_nothing(
    tmp_path: Path, stash: Path,
):
    """A second click is the commonest way a switch destroys state: the
    first pass takes the gateway-only choices out, the second sees a stock
    file and — if it were careless — would overwrite the stash it finds with
    an empty one, so Multimodel would come back empty-handed.

    Asserted on the WHOLE state (settings file + stash bytes), not just the
    no-op status: ``test_remote_control_is_idempotent_and_leaves_the_stash_alone``
    in the sibling file pins the status and the stash mtime; what is pinned
    here is that a Multimodel return after TWO passes restores exactly what a
    return after one would have.
    """
    env = _pointed_env(
        {
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": FLASH_1M,
            "CLAUDE_CODE_SUBAGENT_MODEL": CLAUDE_45,
            "HTTPS_PROXY": "http://corp-proxy:3128",
        },
        model=OPUS_1M,
    )
    path = _settings(
        tmp_path, {"editor.fontSize": 13, vs.ENV_BLOCK_KEY: env, vs.LOGIN_PROMPT_KEY: True},
    )
    start = _doc(path)

    first = vs.set_mode(path, vs.MODE_REMOTE_CONTROL, stash=stash)
    assert first["status"] == "written"
    after_first = _doc(path)
    stash_after_first = json.loads(stash.read_text(encoding="utf-8"))["values"]

    second = vs.set_mode(path, vs.MODE_REMOTE_CONTROL, stash=stash)
    assert second["ok"] and second["status"] == "unchanged", second
    assert _doc(path) == after_first, "the second pass changed the file"
    assert json.loads(stash.read_text(encoding="utf-8"))["values"] == stash_after_first

    back = vs.set_mode(
        path, vs.MODE_MULTIMODEL, base_url=BASE_URL, token=TOKEN, stash=stash,
    )
    assert back["ok"], back
    assert _doc(path) == start, "two passes lost something one pass would have kept"


def test_the_switch_is_still_a_two_state_machine(tmp_path: Path, stash: Path):
    """``panel_mode`` reports the mode each leg just produced.

    The switch working "correctly" starts with the GUI being able to tell
    which side it is on: a pill that reads the wrong state is how a user
    ends up clicking the mode they are already in.
    """
    path = _settings(tmp_path, {"editor.fontSize": 13})
    assert vs.panel_mode(path)["mode"] == vs.MODE_REMOTE_CONTROL, (
        "a file VCO never touched is already the stock client"
    )

    vs.set_mode(path, vs.MODE_MULTIMODEL, base_url=BASE_URL, token=TOKEN, stash=stash)
    assert vs.panel_mode(path)["mode"] == vs.MODE_MULTIMODEL

    vs.set_mode(path, vs.MODE_REMOTE_CONTROL, stash=stash)
    assert vs.panel_mode(path)["mode"] == vs.MODE_REMOTE_CONTROL
