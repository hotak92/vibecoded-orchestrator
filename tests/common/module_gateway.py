# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The delivered module-gateway agent file set — the ONE home (v0.2.96).

`templates/agents/module-gateway/` ships only to projects whose
`model_gateway` module row says active; the engine enumerates it with a
glob (`vco_lib/project_init.py`, `sorted(gateway_agents_src.glob("*.md"))`),
so adding or removing a definition needs zero engine change. The TESTS,
however, name the set explicitly (delivery legs copy it into a fake
orchestrator; contract pins assert on each file), and two files grew
hardcoded copies of the tuple in the WP-10 wave. When the set moved from
the pair to the four-role set (owner ruling 2026-09-21: glm-reviewer is
THE review lane, glm-flash-reviewer retired, flash keeps
research/investigation), every copy would have needed the same edit —
this module is why they don't.

One constant, imported by every test that names the gated set:
`test_install_bundle.py` (ModuleGatewayAgentDeliveryTests),
`test_install_bundle_standalone.py` (launcher-less non-delivery),
`test_v0292_model_gateway_gui_contract.py` (namespace pin, bucket pin,
guidance-render pin). The pin test also asserts the tuple equals the
directory's actual `*.md` listing, so the tuple cannot silently cover
less than the glob ships.
"""

from __future__ import annotations

#: The gated definitions this version ships, in catalog order
#: (strong tier first, flash last). Byte-identical delivery contract:
#: these files carry hardcoded `claude-gw/*` frontmatter ids and land
#: with no placeholder expansion.
MODULE_GATEWAY_AGENT_FILES: tuple[str, ...] = (
    "glm-implementer.md",
    "glm-reviewer.md",
    "glm-planner.md",
    "glm-flash-researcher.md",
)
