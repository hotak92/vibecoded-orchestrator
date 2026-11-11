# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.106 (owner ruling 2026-11-11): the hub manages how long Ollama keeps a
model loaded; VCO's callers send one bounded keep_alive (the fail-safe).

Pins, on the Python side:

* the callers' default keep_alive is ``caller_keep_alive`` from
  ``vco_lib/ollama_model_lifetime.toml`` — the SAME file the Rust core embeds
  (the hub classifies a model as "VCO-loaded" by that value);
* the value is bounded (a model unloads on its own when the hub is down) and
  longer than any idle time the hub accepts (Ollama never unloads first);
* every request path VCO uses carries it: embeds, generate calls and the
  code-embed container's body template;
* ``VCO_OLLAMA_KEEP_ALIVE`` stays the explicit override, and a bare number is
  sent as a JSON number (Ollama parses a string as a Go duration, which needs
  a unit — ``"-1"`` as a string is not "never evict").

The hub's unloader is tested in Rust
(``launcher/src-tauri/vct-hub/src/ollama_lifetime.rs``) against a fake Ollama.
"""
from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest

from vco_lib import ollama_embed_body as body_mod
from vco_lib.ollama_generate import generate_request_body

REPO = Path(__file__).resolve().parent.parent
TABLE = REPO / "vco_lib" / "ollama_model_lifetime.toml"
RUST_CORE = (
    REPO / "launcher" / "src-tauri" / "vct-launcher-core" / "src" / "services" / "ollama_lifetime.rs"
)


def _secs(duration: str) -> int:
    """The Rust core's parser, restated for the test (whole h/m/s or "0")."""
    if duration == "0" or duration.isdigit():
        return int(duration)
    parts = re.findall(r"(\d+)([hms])", duration)
    assert "".join(n + u for n, u in parts) == duration, duration
    return sum(int(n) * {"h": 3600, "m": 60, "s": 1}[u] for n, u in parts)


@pytest.fixture(autouse=True)
def _no_override(monkeypatch):
    monkeypatch.delenv("VCO_OLLAMA_KEEP_ALIVE", raising=False)


def _table() -> dict:
    with TABLE.open("rb") as fh:
        return tomllib.load(fh)


def test_default_is_the_shared_table_value():
    assert body_mod._keep_alive() == _table()["caller_keep_alive"] == "45m"


def test_the_rust_core_embeds_the_same_table():
    src = RUST_CORE.read_text(encoding="utf-8")
    m = re.search(r'include_str!\("([^"]+ollama_model_lifetime\.toml)"\)', src)
    assert m, "the Rust core must embed vco_lib/ollama_model_lifetime.toml"
    assert (RUST_CORE.parent / m.group(1)).resolve() == TABLE.resolve()


def test_caller_keep_alive_is_bounded_and_outlasts_every_hub_idle_time():
    t = _table()
    caller, default, maximum = (_secs(t[k]) for k in ("caller_keep_alive", "default_idle", "max_idle"))
    # Fail-safe: with the hub down a model still unloads within the hour.
    assert 0 < caller <= 3600
    # The hub decides first: Ollama's own timer never fires while it runs.
    assert maximum < caller
    # Unset preference = the pre-v0.2.106 behaviour (10 minutes).
    assert default == 600 <= maximum


def test_every_request_path_carries_the_shared_value():
    want = _table()["caller_keep_alive"]
    assert body_mod.embed_request_body("qwen3-embedding:0.6b", input="x")["keep_alive"] == want
    assert body_mod.embed_request_body("qwen3-embedding:0.6b", prompt="x")["keep_alive"] == want
    assert generate_request_body("qwen3.5:9b", prompt="hi")["keep_alive"] == want
    template = json.loads(body_mod.container_body_template("qwen3-embedding:0.6b", environ={}))
    assert template["keep_alive"] == want


@pytest.mark.parametrize(
    ("raw", "sent"),
    [("-1", -1), ("0", 0), ("3600", 3600), (" 1.5 ", 1.5), ("2h", "2h"), ("30m", "30m")],
)
def test_override_wins_and_bare_numbers_go_out_as_numbers(monkeypatch, raw, sent):
    monkeypatch.setenv("VCO_OLLAMA_KEEP_ALIVE", raw)
    body = body_mod.embed_request_body("qwen3-embedding:0.6b", input="x")
    assert body["keep_alive"] == sent
    assert type(body["keep_alive"]) is type(sent)
    # On the wire: "-1" must be the number -1, never the string "-1".
    wire = json.dumps(body)
    assert f'"keep_alive": {json.dumps(sent)}' in wire


def test_empty_override_sends_no_keep_alive(monkeypatch):
    monkeypatch.setenv("VCO_OLLAMA_KEEP_ALIVE", "")
    assert "keep_alive" not in body_mod.embed_request_body("m", input="x")
    assert "keep_alive" not in generate_request_body("m", prompt="hi")


def test_container_template_honours_a_numeric_override():
    template = json.loads(
        body_mod.container_body_template(
            "qwen3-embedding:0.6b", environ={"VCO_OLLAMA_KEEP_ALIVE": "-1"}
        )
    )
    assert template["keep_alive"] == -1
