# SPDX-License-Identifier: AGPL-3.0-or-later
"""v0.2.102 W6-J — the hub `/config` resolver's text-embedding answer.

Until W6-J the hub's ``embedding_models.text`` read
``project_kg_bindings.embedding_model`` — the column the retired per-project
KG model picker wrote. With the picker gone the answer derives from the
project's RESOLVED active profile, through a rung-C mirror of the Python SSOT
``vco_lib.embedding_selection.model_id_for_active`` (the hub crate cannot call
Python on its per-request path and carries no shared table for this map).
This parity test is the lock the mirror's "keep them equal" comment names:
every profile the match names, plus its default arm, must answer exactly what
the SSOT answers.
"""
from __future__ import annotations

import re
from pathlib import Path

from vco_lib.embedding_selection import model_id_for_active

REPO = Path(__file__).resolve().parents[1]
CONFIG_API = REPO / "launcher" / "src-tauri" / "vct-hub" / "src" / "config_api.rs"


def _hub_text_map() -> "tuple[dict[str, str], str]":
    src = CONFIG_API.read_text(encoding="utf-8")
    anchor = "let text_embedding = match active_embedding.as_str() {"
    start = src.index(anchor) + len(anchor)
    block = src[start:src.index("}", start)]
    arms = dict(re.findall(r'"([^"]+)"\s*=>\s*"([^"]+)"', block))
    default = re.search(r'_\s*=>\s*"([^"]+)"', block)
    assert default, "the hub match lost its default arm"
    return arms, default.group(1)


def test_hub_profile_to_text_model_matches_the_python_ssot():
    arms, default = _hub_text_map()
    assert arms, "no profile arms parsed from config_api.rs"
    for profile, model in arms.items():
        assert model == model_id_for_active(profile), profile
    # The default arm answers what the SSOT answers for any other profile
    # (codesage, an unvalidated app_state value, ...).
    for other in ("qwen3", "codesage", "not-a-profile"):
        if other not in arms:
            assert default == model_id_for_active(other), other


def test_every_ssot_profile_with_its_own_model_has_a_hub_arm():
    arms, default = _hub_text_map()
    for profile in ("arctic", "openai"):
        assert model_id_for_active(profile) != default
        assert profile in arms, f"the hub would answer {default} for {profile}"
