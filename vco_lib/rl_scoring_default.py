# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""What RL scoring defaults to when the user has not chosen — the ONE home.

Owner ruling (2026-11-12): "RL scoring default follows the bundled weights".

* An explicit user choice — the project's own toggle, else the host-wide
  default row — always wins, ON or OFF.
* With no choice set, RL scoring is ON only when a trained model is bundled
  for the ACTIVE embedding profile: the bundled-weights manifest
  (``claude_mcp_servers/rl_reranker/weights/manifest.json``, read only through
  ``rl_reranker.bundled_weights``) names a ``current`` version for that
  profile. None ships today, so the default is OFF for every profile.
* A manifest that is missing, unreadable or inconsistent, or a profile it does
  not list, resolves to OFF (conservative: "a trained model ships" cannot be
  positively confirmed).

Readers
-------
* Rust (tier A of the A>B>C rule): ``vct-launcher-core/src/rl_scoring_default.rs``
  runs ``python -m vco_lib.rl_scoring_default --profile <p> --json`` once per
  profile and caches the answer. ``Db::rl_scoring_enabled_for_project`` (the
  hub's ``/config`` field ``rl_reranker_enabled_for_project``), the module
  enable cascade and the launcher GUI's default text all come from it.
* Python: the search pipeline, ``project_config``, the CLAUDE.md section gate
  and ``rl_doctor`` read the EFFECTIVE value the hub serves; a toggle they
  cannot read (hub down, an older hub) stays OFF, because the explicit choice
  that would win cannot be confirmed. ``rl_doctor`` also reports
  :func:`rl_scoring_default` for the active profile.

The decision is :func:`rl_scoring_enabled`; :func:`rl_scoring_default` is its
"nothing chosen" leg.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

#: The bundled-weights root in this checkout.
WEIGHTS_ROOT: Path = (
    Path(__file__).resolve().parent.parent / "claude_mcp_servers" / "rl_reranker" / "weights"
)


@dataclass(frozen=True)
class RlScoringDefault:
    """The "nothing chosen" answer for one embedding profile."""

    profile: str
    #: A trained model ships for ``profile`` (the manifest names a ``current``).
    bundled_model: bool
    #: Why ``bundled_model`` is False when the manifest could not be read;
    #: ``None`` when it was read (whatever it said).
    error: Optional[str] = None

    @property
    def default_on(self) -> bool:
        return self.bundled_model


def bundled_model_ships(profile: str, weights_root: Optional[Path] = None) -> RlScoringDefault:
    """Whether the bundled-weights manifest names a trained model for ``profile``.

    File presence and sha256 are verified by the image build and the server
    (``bundled_weights.resolve_bundled_weights``); this answers the manifest's
    declaration only, which is what ships with the release.
    """
    from claude_mcp_servers.rl_reranker.bundled_weights import (  # noqa: PLC0415
        BundledWeightsError,
        load_manifest,
    )

    root = WEIGHTS_ROOT if weights_root is None else Path(weights_root)
    profile = (profile or "").strip()
    if not profile:
        return RlScoringDefault(profile="", bundled_model=False, error="no active embedding profile")
    try:
        doc = load_manifest(root)
    except BundledWeightsError as exc:
        return RlScoringDefault(profile=profile, bundled_model=False, error=str(exc))
    if doc is None:
        return RlScoringDefault(
            profile=profile, bundled_model=False, error=f"no bundled-weights manifest under {root}"
        )
    entry = doc["profiles"].get(profile)
    if entry is None:
        return RlScoringDefault(profile=profile, bundled_model=False)
    return RlScoringDefault(profile=profile, bundled_model=entry.get("current") is not None)


def rl_scoring_default(profile: str, weights_root: Optional[Path] = None) -> bool:
    """RL scoring's default for ``profile`` when the user has not chosen."""
    return bundled_model_ships(profile, weights_root).default_on


def rl_scoring_enabled(
    explicit: Optional[bool], profile: str, weights_root: Optional[Path] = None
) -> bool:
    """The decision: an explicit choice wins; else the bundled-weights default."""
    if explicit is not None:
        return bool(explicit)
    return rl_scoring_default(profile, weights_root)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m vco_lib.rl_scoring_default",
        description="RL scoring's default for an embedding profile (bundled trained model or not).",
    )
    parser.add_argument("--profile", required=True, help="active embedding profile, e.g. qwen3")
    parser.add_argument("--json", action="store_true", help="print one JSON object")
    args = parser.parse_args(argv)
    answer = bundled_model_ships(args.profile)
    if args.json:
        payload = asdict(answer)
        payload["default_on"] = answer.default_on
        print(json.dumps(payload, sort_keys=True))
    else:
        state = "on" if answer.default_on else "off"
        why = "trained model bundled" if answer.bundled_model else "no trained model bundled"
        print(f"{answer.profile}: default {state} ({why})")
        if answer.error:
            print(f"warning: {answer.error}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
