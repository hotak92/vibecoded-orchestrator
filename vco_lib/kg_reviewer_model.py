# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Which Claude model the post-commit KG reviewer runs (v0.2.103).

ONE machine-wide setting (owner ruling 2026-10-15: global, not per project;
default Sonnet, choices Haiku / Sonnet / Opus). The rule table is
``vco_lib/kg_reviewer_config.toml`` — parsed here and by the launcher's
``commands/kg_reviewer_model.rs`` (tier B of A>B>C), so the Preferences
selector and this resolver can never offer different choices.

Precedence (first hit wins; :func:`resolve` returns the model AND the rung):

1. ``VCO_KG_REVIEW_MODEL`` — the operator's env override (beside
   ``VCO_KG_REVIEW_MAX_TURNS``);
2. launcher Preferences -> KG commit reviewer
   (``app_state["kg_reviewer.model"]``);
3. the table's ``default`` (``sonnet``).

A value outside the table's ``choices`` is never passed to the CLI: it is
reported through ``log`` and the next rung answers.

CLI (debugging)::

    python -m vco_lib.kg_reviewer_model [--json]
"""
from __future__ import annotations

import argparse
import functools
import json
import os
import sys
import tomllib
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence, Tuple

__all__ = [
    "CONFIG_PATH",
    "FORMAT_VERSION",
    "config",
    "model_choices",
    "default_model",
    "gateway_rung_models",
    "resolve",
]

CONFIG_PATH = Path(__file__).resolve().parent / "kg_reviewer_config.toml"
#: The table schema this loader understands — MUST MATCH
#: ``launcher/src-tauri/src/commands/kg_reviewer_model.rs``.
FORMAT_VERSION = 1

SOURCE_ENV = "env"
SOURCE_PREFERENCES = "preferences"
SOURCE_DEFAULT = "default"


@functools.lru_cache(maxsize=1)
def config() -> "dict[str, Any]":
    """The parsed rule table. Raises on a missing/malformed table — it ships
    inside ``vco_lib``, so a failure here is a broken install."""
    data = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    if data.get("format_version") != FORMAT_VERSION:
        raise RuntimeError(
            f"{CONFIG_PATH} format_version {data.get('format_version')!r} "
            f"!= {FORMAT_VERSION} (this loader)")
    return data


def model_choices() -> "tuple[str, ...]":
    return tuple(config()["model"]["choices"])


def default_model() -> str:
    return str(config()["model"]["default"])


def env_key() -> str:
    return str(config()["model"]["env"])


def app_state_key() -> str:
    return str(config()["model"]["app_state_key"])


def gateway_rung_models() -> "tuple[tuple[str, str], ...]":
    """The non-flash fallback rungs as ``(vendor row id, bare model id)``."""
    return tuple((str(v), str(m)) for v, m in config()["fallback"]["gateway_rungs"])


def _preferences_value() -> Optional[str]:
    from vco_lib.launcher_db_reader import read_app_state_value

    return read_app_state_value(app_state_key())


def _normalise(raw: Optional[str]) -> str:
    return (raw or "").strip().lower()


def resolve(
    env: Optional[Mapping[str, str]] = None,
    *,
    preferences: Optional[Callable[[], Optional[str]]] = None,
    log: Optional[Callable[[str], None]] = None,
) -> Tuple[str, str]:
    """``(model, source)`` — see the module docstring for the precedence.
    ``preferences`` is a seam for tests; production passes none."""
    env = os.environ if env is None else env
    log = log or (lambda line: print(f"[kg-reviewer] {line}", file=sys.stderr))
    choices = model_choices()
    override = _normalise(env.get(env_key(), ""))
    if override:
        if override in choices:
            return override, SOURCE_ENV
        log(f"{env_key()}={override!r} is not one of {', '.join(choices)} "
            f"- ignored")
    try:
        pick = _normalise((preferences or _preferences_value)())
    except Exception as exc:  # noqa: BLE001 — an unreadable DB is "unset"
        log(f"launcher preference {app_state_key()} unreadable ({exc}) - "
            f"using the default")
        pick = ""
    if pick:
        if pick in choices:
            return pick, SOURCE_PREFERENCES
        log(f"launcher preference {app_state_key()}={pick!r} is not one of "
            f"{', '.join(choices)} - ignored")
    return default_model(), SOURCE_DEFAULT


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m vco_lib.kg_reviewer_model",
        description="Print the KG commit reviewer's model and which rule chose it.")
    p.add_argument("--json", action="store_true",
                   help='one JSON object: {"model", "source", "choices", "default"}')
    args = p.parse_args(list(sys.argv[1:] if argv is None else argv))
    model, source = resolve()
    if args.json:
        sys.stdout.write(json.dumps({"model": model, "source": source,
                                     "choices": list(model_choices()),
                                     "default": default_model()}) + "\n")
    else:
        sys.stdout.write(f"{model}\t({source})\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
