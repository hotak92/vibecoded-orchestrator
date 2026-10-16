# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Runtime resolver for the local summary / generation model (v0.2.102 W5-D).

The question: which Ollama model does THIS machine's generation ladder ask
for? Before W5-D the answer was a constant (``qwen3.5:9b``) while install
pulled the hardware tier's model (``gemma4:e4b`` on 6-16 GB VRAM or a capable
CPU, ``qwen3.5:0.8b`` as the floor) — so on those machines the local summary
tier asked for a model that was never pulled and was skipped. Now ONE
resolver answers, from ONE tier table
(:func:`vco_lib.embedding_selection.summary_model_for_hardware`, the same
function install's pull list is derived from), and every generate caller —
``summary_backends`` (KG summaries, incl. ``generate-kg-summary``'s per-node
and bulk modes through it) and ``process_documents`` — reads its answer
through ``summary_backends.OLLAMA_DEFAULT_MODEL``, so one machine keeps ONE
generation model resident.

Precedence (first hit wins; :func:`resolve` returns the model AND which
rung answered):

1. ``KG_SUMMARY_OLLAMA_MODEL`` — the operator's env override.
2. launcher Preferences → KG Summaries → "Ollama model"
   (``app_state[kg_summary_ollama_model]``; empty = Auto-select). The
   launcher has written this key since v0.2.23; until W5-D nothing read it.
3. what install RECORDED: the text-generation model of the install profile
   (``<orchestrator>/.claude/state/ollama_pull_profile.json`` — the profile
   cap, else the hardware tier; first entry, exactly the model install pulled,
   :mod:`vco_lib.embedding_pull_plan`).
4. the hardware-tier probe (:mod:`vco_lib.host_probe`) through the same
   table — for an install older than the record.

The resolver never checks Ollama: whether the resolved model is actually
present is the Ollama tier's own availability probe
(``summary_backends.ollama_available``), which says so on stderr and moves the
ladder on — nothing is ever pulled at runtime.

CLI (debugging, and the launcher's display of the resolved model)::

    python -m vco_lib.summary_model [--json]

``--json`` prints ``{"model", "source", "tiers"}``; ``tiers`` is the tier
table as data (:func:`vco_lib.embedding_selection.summary_model_tiers`), which
the launcher's Preferences page renders as its "No Ollama models installed"
hint (``get_summary_model_info``).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence, Tuple

__all__ = [
    "APP_STATE_KEY_OLLAMA_MODEL",
    "ENV_OLLAMA_MODEL",
    "recorded_generation_model",
    "resolve",
    "resolve_model",
]

ENV_OLLAMA_MODEL = "KG_SUMMARY_OLLAMA_MODEL"
#: Written by the launcher's Preferences page (``preferences/loaders.ts``,
#: ``APP_STATE_KEY_KG_SUMMARY_OLLAMA_MODEL`` — MUST MATCH).
APP_STATE_KEY_OLLAMA_MODEL = "kg_summary_ollama_model"

SOURCE_ENV = "env"
SOURCE_PREFERENCES = "preferences"
SOURCE_INSTALL_RECORD = "install-record"
SOURCE_HARDWARE = "hardware-probe"


def _orchestrator_roots() -> Sequence[Path]:
    """Where the install record may live: ``VCT_INSTALL_ROOT`` when set, then
    the checkout this ``vco_lib`` is imported from (install.py writes the
    record under its own root, which is that checkout)."""
    roots = []
    env_root = os.environ.get("VCT_INSTALL_ROOT", "").strip()
    if env_root:
        roots.append(Path(env_root))
    roots.append(Path(__file__).resolve().parent.parent)
    return roots


def recorded_generation_model(roots: Optional[Sequence[Path]] = None) -> Optional[str]:
    """The text-generation model install recorded (and pulled), or None."""
    from vco_lib.embedding_pull_plan import read_record

    for root in (roots if roots is not None else _orchestrator_roots()):
        rec = read_record(root)
        if not rec:
            continue
        ladder = rec.get("profile_override") or rec.get("capability_tier") or []
        if isinstance(ladder, list) and ladder and isinstance(ladder[0], str) and ladder[0].strip():
            return ladder[0].strip()
    return None


def _preferences_model() -> Optional[str]:
    from vco_lib.launcher_db_reader import read_app_state_value

    value = read_app_state_value(APP_STATE_KEY_OLLAMA_MODEL)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _hardware_model() -> str:
    from vco_lib.embedding_selection import summary_model_for_hardware
    from vco_lib.host_probe import probe_cpu_cores, probe_gpu_vram_gb, probe_system_ram_gb

    return summary_model_for_hardware(probe_gpu_vram_gb(), probe_system_ram_gb(), probe_cpu_cores())


def resolve(
    env: Optional[Mapping[str, str]] = None,
    *,
    preferences: Optional[Callable[[], Optional[str]]] = None,
    recorded: Optional[Callable[[], Optional[str]]] = None,
    hardware: Optional[Callable[[], str]] = None,
) -> Tuple[str, str]:
    """``(model, source)`` — see the module docstring for the precedence.
    The three callables are seams for tests; production passes none."""
    env = os.environ if env is None else env
    override = str(env.get(ENV_OLLAMA_MODEL, "") or "").strip()
    if override:
        return override, SOURCE_ENV
    pick = (preferences or _preferences_model)()
    if pick:
        return pick, SOURCE_PREFERENCES
    rec = (recorded or recorded_generation_model)()
    if rec:
        return rec, SOURCE_INSTALL_RECORD
    return (hardware or _hardware_model)(), SOURCE_HARDWARE


def resolve_model() -> str:
    """Just the model (what ``summary_backends.OLLAMA_DEFAULT_MODEL`` holds)."""
    return resolve()[0]


def build_parser() -> argparse.ArgumentParser:


    p = argparse.ArgumentParser(
        prog="python -m vco_lib.summary_model",
        description="Print the machine's local summary/generation model and which rule chose it.")
    p.add_argument("--json", action="store_true",
                   help='one JSON object: {"model", "source", "tiers"}')
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    from vco_lib.embedding_selection import summary_model_tiers

    model, source = resolve()
    if args.json:
        sys.stdout.write(json.dumps({"model": model, "source": source,
                                     "tiers": summary_model_tiers()}) + "\n")
    else:
        sys.stdout.write(f"{model}\t({source})\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
