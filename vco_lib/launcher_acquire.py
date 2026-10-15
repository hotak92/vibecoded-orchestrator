# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The ONE home for how a first install gets its launcher binary when no
usable one is on disk (owner rule 2026-10-15, plan item 29).

THE RULE: when a prebuilt launcher exists for this OS, arch and VCO version,
it is downloaded WITHOUT asking. The user is asked about compiling from
source only when no prebuilt could be obtained. An unattended run
(``--yes``) builds in that case, exactly as before.

Before this, both shims (``first-install.bat`` and
``scripts/post-install-launcher.sh``) opened with a three-way menu
("download prebuilt / build from source / skip") even when the release
asset was there, so every Windows user whose bundled binary was judged
stale was asked a question with one sensible answer.

The shims own the mechanics (probing, downloading, unpacking, building);
this module owns the two decisions they share, through a tiny CLI
(A>B>C rule A — one implementation, called from cmd and bash):

  * ``api-url`` — the GitHub Releases endpoint for THIS checkout's version
    (``releases/tags/v<version>``), so "a prebuilt exists" means one for
    this version, not whatever release is newest;
  * ``next-step --outcome <downloaded|no-prebuilt> [--yes]`` — what the
    shim does after its download attempt: ``launch`` (the prebuilt
    arrived; nothing is asked), ``build`` (no prebuilt, unattended), or
    ``ask`` (no prebuilt, interactive: ask "build from source or skip?").

Both shims fall back to ``ask`` when this CLI cannot run at all: when the
rule itself cannot be consulted, a question is the conservative default.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

RELEASES_API = "https://api.github.com/repos/hotak92/vibecoded-orchestrator/releases"

OUTCOME_DOWNLOADED = "downloaded"
OUTCOME_NO_PREBUILT = "no-prebuilt"
OUTCOMES = (OUTCOME_DOWNLOADED, OUTCOME_NO_PREBUILT)

STEP_LAUNCH = "launch"
STEP_BUILD = "build"
STEP_ASK = "ask"


def checkout_version(root: Optional[Path] = None) -> Optional[str]:
    """The version this checkout ships (``[project].version`` in
    ``pyproject.toml``, the field ``scripts/bump-version.sh`` moves), or
    ``None`` when it cannot be read."""
    import tomllib

    base = Path(root) if root is not None else Path(__file__).resolve().parent.parent
    try:
        with open(base / "pyproject.toml", "rb") as fh:
            version = tomllib.load(fh).get("project", {}).get("version")
    except (OSError, ValueError):
        return None
    return str(version).strip() or None if version else None


def release_api_url(version: Optional[str]) -> str:
    """The Releases API URL for *version* (``tags/v<version>``); the newest
    release only when the version is unknown."""
    if not version:
        return f"{RELEASES_API}/latest"
    tag = version if version.startswith("v") else f"v{version}"
    return f"{RELEASES_API}/tags/{tag}"


def next_step(outcome: str, *, yes: bool) -> str:
    """What the shim does after its download attempt (module docstring)."""
    if outcome == OUTCOME_DOWNLOADED:
        return STEP_LAUNCH
    return STEP_BUILD if yes else STEP_ASK


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m vco_lib.launcher_acquire",
        description="How a first install obtains the launcher binary.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_url = sub.add_parser("api-url", help="Releases API URL for this checkout's version")
    p_url.add_argument("--root", default=None, help="checkout root (default: this vco_lib's)")
    p_step = sub.add_parser("next-step", help="the step after the download attempt")
    p_step.add_argument("--outcome", required=True, choices=OUTCOMES)
    p_step.add_argument("--yes", action="store_true", help="unattended run")
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    if args.cmd == "api-url":
        print(release_api_url(checkout_version(Path(args.root) if args.root else None)))
        return 0
    print(next_step(args.outcome, yes=args.yes))
    return 0


if __name__ == "__main__":
    sys.exit(main())
