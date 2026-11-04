# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.105 fix round 2, item 6 — every shipped gateway agent definition
says how waiting works.

Field report 2026-10-30: a glm-implementer lane "waited" for its own
background script by calling Bash ``true`` 1 310 times. The guard refuses
that (``vco_lib.wait_guard.noop_repeat_key``), and the definition now says
the rule in its own words: waiting means ENDING YOUR TURN and being woken
by the completion notification, never calling a tool in a loop.

One line per definition; every other line of the shipped text stays as it
was (the frontmatter still parses and still pins a gateway model).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.common.module_gateway import MODULE_GATEWAY_AGENT_FILES

GATED_SRC = Path(__file__).resolve().parents[1] / "templates" / "agents" / "module-gateway"

WAITING_LINE = (
    "- Waiting: waiting means ENDING YOUR TURN and being woken by the "
    "completion notification (start long work with `run_in_background`), "
    "never calling a tool in a loop to check on it."
)


@pytest.mark.parametrize("name", MODULE_GATEWAY_AGENT_FILES)
def test_every_gateway_definition_says_how_to_wait(name):
    text = (GATED_SRC / name).read_text(encoding="utf-8")
    assert WAITING_LINE in text, name


@pytest.mark.parametrize("name", MODULE_GATEWAY_AGENT_FILES)
def test_the_rest_of_each_definition_is_untouched(name):
    text = (GATED_SRC / name).read_text(encoding="utf-8")
    # The frontmatter still closes, still names the agent and still pins a
    # gateway model — the added line lives in the body, not the header.
    assert text.startswith("---\n")
    front = text.split("---\n", 2)
    assert front[1].count("\nname:") == 1 or front[1].startswith("name:")
    assert re.search(r"^name: %s$" % re.escape(name.removesuffix(".md")),
                     front[1], re.M), name
    assert re.search(r"^model: claude-gw/", front[1], re.M), name
    # Exactly ONE waiting line: a second copy would drift from the first.
    assert text.count("Waiting: waiting means ENDING YOUR TURN") == 1, name