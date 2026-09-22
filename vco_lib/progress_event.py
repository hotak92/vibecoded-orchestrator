# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The ``[VCO-EVENT]`` progress-line grammar — the ONE Python home.

MUST MATCH the consumer in
``launcher/src-tauri/src/commands/update_pipeline.rs`` (``read_stdout``,
the ``line.strip_prefix("[VCO-EVENT] ")`` + ``rest.splitn(3, ' ')`` block)
and the relay filter in ``vco_lib/child_process.py`` (``_EVENT_LINE_RE``).
Parity is enforced by ``tests/test_v0296_lane_python_core.py::VcoEventLineGrammarParity``,
which reads the Rust source and compares it against the constants here —
not a marker-string scan, which a name in a comment satisfies.

THE GRAMMAR (one line, no JSON — the consumer str-splits it, deliberately,
so a per-tick JSON parse never lands on the update's hot path)::

    [VCO-EVENT] <step> <phase> <detail...>
    └ PREFIX ──┘ └─ exactly 3 whitespace-separated fields, detail last ─┘

* ``step`` — an installer step token (``7c/10``) or a producer-specific one
  (``kg-sync``). The launcher maps known tokens to user labels and falls back
  to showing ``detail`` verbatim for unknown ones.
* ``phase`` — only ``start`` and ``ok`` become GUI sub-messages; every other
  phase is silent on that surface (it is already in the JSONL log).
* ``detail`` — free text, **newline-free** (the consumer is line-buffered, so
  an embedded newline would split one event into two malformed ones).

WHY THE MIRROR EXISTS (v0.2.49 batch 4, recorded here with the code that
serves it): the launcher's ``OrchestratorUpdateProgressModal`` otherwise sits
at 40 % "Applying updates…" for the whole re-embedding phase — minutes of
apparent freeze — because ``install.py`` prints nothing while it blocks on
its children. These ticks are what turns that into visible sub-progress, and
they are what the update stall watchdog counts as liveness.

Two producers call this: ``install.py::_log_install_event``'s stdout mirror
and ``templates/scripts/sync_knowledge_graph.py::_emit_sync_event`` (the
child's throttled heartbeat, which additionally caps ``detail`` length and
tracks its own throttle clock — producer policy, not grammar). Before
v0.2.96 each formatted the line itself, unlocked, with the Rust side carrying
no "must match" comment at all.

Soft-fail is absolute: a progress tick must never fail the operation it is
reporting on.
"""

from __future__ import annotations

import os
import sys

__all__ = ["EVENT_PREFIX", "EVENT_FIELD_COUNT", "STREAM_ENV", "format_event", "emit"]

#: The literal line prefix, INCLUDING its trailing space. The Rust consumer
#: strips exactly this; a change here without the same change there silently
#: stops every progress tick reaching the GUI.
EVENT_PREFIX = "[VCO-EVENT] "

#: Fields after the prefix: ``step``, ``phase``, ``detail``. The Rust side
#: uses ``splitn(3, ' ')``, so ``detail`` keeps its own spaces and only the
#: first two separators are structural.
EVENT_FIELD_COUNT = 3

#: The env gate. The launcher is the ONLY setter (``update_pipeline.rs``);
#: a human terminal run never sets it, so CLI output stays quiet. Compared
#: with ``== "1"`` exactly — not truthiness — at every producer.
STREAM_ENV = "VCO_PROGRESS_STREAM"


def format_event(step: str, phase: str, detail: str) -> str:
    """Render one ``[VCO-EVENT]`` line, newline-terminated.

    ``detail``'s CR/LF are replaced with spaces: the consumer reads
    line-by-line, so an embedded newline would split one event into two, the
    second of which has no prefix and is dropped — silently losing the tick
    AND, worse, emitting a half-event that parses as a different step.
    """
    safe_detail = (detail or "").replace("\n", " ").replace("\r", " ")
    return f"{EVENT_PREFIX}{step} {phase} {safe_detail}\n"


def emit(step: str, phase: str, detail: str = "") -> None:
    """Write one event line to stdout when the stream gate is on.

    No-op when ``VCO_PROGRESS_STREAM`` is not exactly ``"1"``. Never raises:
    a closed pipe or a disconnected terminal is not a reason to break the
    install, and the JSONL log the caller already wrote is the durable record.
    """
    if os.environ.get(STREAM_ENV) != "1":
        return
    try:
        sys.stdout.write(format_event(step, phase, detail))
        sys.stdout.flush()
    except Exception:  # noqa: BLE001 — a progress tick never fails its caller
        pass
