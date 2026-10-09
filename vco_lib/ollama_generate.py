# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""ONE home for the body of a VCO Ollama GENERATE request (v0.2.102 W4-G).

Owner rule (2026-10-08): projects share ONE copy of each model. Ollama loads
a SECOND runner of the same model whenever a request's LOAD-TIME options
differ from the resident runner's (its reload check compares the runner
options: ``num_ctx``, ``num_batch``, ``num_gpu``, ``main_gpu``, ``use_mmap``,
``num_thread``). W4-E made every EMBED request identical per model
(``vco_lib.embedding_providers.ollama.embed_request_body``); this module does
the same for generation. W4-E measured the split it closes: the KG-summary
ladder sent ``qwen3.5:9b`` with ``num_ctx: 32768`` while the node-formats
generator and the document summariser sent none, so the same model was
resident twice.

Every ``/api/generate`` caller builds its body here:

* ``templates/scripts/summary_backends.py::call_ollama`` (KG-summary ladder,
  also the code-summary generator through it);
* ``claude_mcp_servers/scripts/generate_node_formats.py::call_ollama``;
* ``templates/scripts/process_documents.py::generate_document_summary``.

Sampling options (temperature, top_p, num_predict, …) never trigger a
reload and stay per caller. Load-time options are owned HERE: one context
window for every generation model (:func:`generate_num_ctx`, default
:data:`GENERATE_NUM_CTX_DEFAULT`, ``$VCO_OLLAMA_GENERATE_NUM_CTX`` overrides
for every caller at once), and any load-time key a caller passes is dropped
(with one stderr note when it differed) — a per-caller window is exactly the
divergence this module exists to prevent. The shared ``keep_alive`` policy
is applied through its own one home (``_with_keep_alive``).
"""

from __future__ import annotations

import os
import sys
from typing import Any, Mapping, Optional

from vco_lib.embedding_providers.ollama import _with_keep_alive

#: The context window every VCO generation request asks for. 32768 is the
#: value the KG-summary ladder already loaded its models with (~3 × 8k input
#: chunks + system prompt + output), so the resident runner it created is the
#: one every other caller now reuses.
GENERATE_NUM_CTX_DEFAULT = 32768

#: Machine-wide override of :data:`GENERATE_NUM_CTX_DEFAULT` (a positive int).
#: It applies to EVERY generation caller at once, so it can never split a
#: model into two runners the way a per-caller window did.
GENERATE_NUM_CTX_ENV = "VCO_OLLAMA_GENERATE_NUM_CTX"

#: Ollama options that are part of the runner's load identity — a request
#: whose value differs from the resident runner's makes Ollama load another
#: copy. Owned by this module, never by a caller.
LOAD_TIME_OPTIONS = frozenset(
    {"num_ctx", "num_batch", "num_gpu", "main_gpu", "use_mmap", "num_thread"}
)

_warned: set[str] = set()


def generate_num_ctx() -> int:
    """The one generation context window (env override, else the default).

    A blank, non-integer or non-positive override is ignored with a stderr
    note — never half-applied.
    """
    raw = (os.environ.get(GENERATE_NUM_CTX_ENV) or "").strip()
    if raw:
        try:
            val = int(raw)
        except ValueError:
            val = 0
        if val > 0:
            return val
        _note(f"ignoring {GENERATE_NUM_CTX_ENV}={raw!r} (not a positive integer); "
              f"using {GENERATE_NUM_CTX_DEFAULT}")
    return GENERATE_NUM_CTX_DEFAULT


def _note(msg: str) -> None:
    if msg in _warned:
        return
    _warned.add(msg)
    print(f"[vco ollama-generate] {msg}", file=sys.stderr)


def generate_request_body(
    model: str,
    *,
    prompt: str,
    system: Optional[str] = None,
    options: Optional[Mapping[str, Any]] = None,
    think: Optional[bool] = None,
    stream: bool = False,
) -> dict[str, Any]:
    """Build the ``/api/generate`` body for *model*.

    ``options`` carries the caller's SAMPLING options; load-time keys in it
    are replaced by this module's values (a differing one is reported once on
    stderr). ``think`` is sent only when not ``None``; ``system`` only when
    given.
    """
    opts: dict[str, Any] = {}
    num_ctx = generate_num_ctx()
    for key, value in dict(options or {}).items():
        if key in LOAD_TIME_OPTIONS:
            want = num_ctx if key == "num_ctx" else None
            if value != want:
                _note(
                    f"dropping caller option {key}={value!r} for {model}: load-time "
                    "options are shared by every caller so the model stays ONE "
                    f"resident copy (set {GENERATE_NUM_CTX_ENV} to change the window)"
                    if key == "num_ctx" else
                    f"dropping caller option {key}={value!r} for {model}: load-time "
                    "options are shared by every caller so the model stays ONE "
                    "resident copy"
                )
            continue
        opts[key] = value
    opts["num_ctx"] = num_ctx
    body: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "stream": bool(stream),
        "options": opts,
    }
    if system is not None:
        body["system"] = system
    if think is not None:
        body["think"] = bool(think)
    return _with_keep_alive(body)
