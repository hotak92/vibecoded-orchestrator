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

  * ``pick-asset --os <linux|macos> --release-json <file>`` — which
    release asset the POSIX shim downloads for this OS/arch, and the URL of
    its ``.sha256`` sidecar (the release document is read from a FILE: an
    environment variable or argv entry larger than 128 KiB makes ``execve``
    fail with E2BIG, R20 L2);
  * ``verify-sha256 --file <archive> --sidecar <file> [--name <asset>]`` —
    whether a downloaded archive matches the digest release.yml published
    beside it (R20 M1). Both shims call it BEFORE unpacking or running
    anything.

Both shims fall back to ``ask`` when this CLI cannot run at all: when the
rule itself cannot be consulted, a question is the conservative default.

A download that cannot be VERIFIED — the sidecar is absent from the
release, could not be fetched, is malformed, or names a different digest —
is not a prebuilt the shim may run: it counts as "no prebuilt obtained"
(``next-step --outcome no-prebuilt``), so the user is asked about compiling
(``--yes``: built from source), exactly like a release with no asset.
"""

from __future__ import annotations

import argparse
import json
import platform
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

RELEASES_API = "https://api.github.com/repos/hotak92/vibecoded-orchestrator/releases"

OUTCOME_DOWNLOADED = "downloaded"
OUTCOME_NO_PREBUILT = "no-prebuilt"
OUTCOMES = (OUTCOME_DOWNLOADED, OUTCOME_NO_PREBUILT)

STEP_LAUNCH = "launch"
STEP_BUILD = "build"
STEP_ASK = "ask"

#: verify-sha256 exit codes. MISMATCH is a code nothing else produces: 1 is a
#: Python crash (traceback, broken venv, failed vco_lib import) and 2 is an
#: argparse error, and neither may be reported to the user as a mismatch.
#: download-launcher-asset.ps1 maps these codes — must match it.
VERIFY_OK = 0
VERIFY_UNVERIFIABLE = 2  # sidecar missing / unreadable / malformed
VERIFY_MISMATCH = 3

#: pick-asset exit codes.
PICK_OK = 0
PICK_NO_ASSET = 2
PICK_UNREADABLE = 3

SIDECAR_SUFFIX = ".sha256"

_HEX64_RE = re.compile(r"^[0-9a-fA-F]{64}$")


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


def parse_sha256_sidecar(text: str, expected_name: Optional[str] = None) -> Optional[str]:
    """The lower-case hex digest a ``sha256sum``/``shasum -a 256`` sidecar
    states (``<hex>  <name>`` or ``<hex> *<name>``), else ``None``.

    ``None`` too when the sidecar names a DIFFERENT file than
    *expected_name*: a digest for another archive verifies nothing."""
    for line in text.splitlines():
        parts = line.strip().split(None, 1)
        if not parts:
            continue
        digest = parts[0]
        if not _HEX64_RE.match(digest):
            return None
        if expected_name and len(parts) == 2:
            named = parts[1].strip().lstrip("*")
            named = named.replace("\\", "/").rsplit("/", 1)[-1]
            if named != expected_name:
                return None
        return digest.lower()
    return None


def verify_sha256(archive: Path, sidecar: Path,
                  name: Optional[str] = None) -> Tuple[int, str]:
    """``(VERIFY_*, message)`` for *archive* against *sidecar* (R20 M1)."""
    from vco_lib.hashing import sha256_file

    try:
        text = Path(sidecar).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return VERIFY_UNVERIFIABLE, f"checksum sidecar unreadable ({exc})"
    expected = parse_sha256_sidecar(text, name)
    if expected is None:
        return VERIFY_UNVERIFIABLE, (
            "checksum sidecar is malformed or names another file"
            + (f" (expected {name})" if name else ""))
    try:
        actual = sha256_file(Path(archive))
    except OSError as exc:
        return VERIFY_UNVERIFIABLE, f"downloaded archive unreadable ({exc})"
    if actual != expected:
        return VERIFY_MISMATCH, (
            f"SHA-256 mismatch: release states {expected}, download is {actual}"
            " (truncated/corrupted download or a proxy page)")
    return VERIFY_OK, f"SHA-256 verified ({actual})"


def pick_asset(release: Dict[str, Any], os_name: str,
               arch: Optional[str] = None) -> Optional[Dict[str, str]]:
    """The asset the POSIX shim downloads for *os_name*/*arch* from a GitHub
    release document — ``{"url", "name", "sha256_url"}`` (``sha256_url`` is
    ``""`` when the release carries no sidecar for it) — else ``None``.

    Moved verbatim from the inline picker in post-install-launcher.sh
    (M-P0-3, v0.2.53): ``.zip`` first (release.yml's packaging since
    2026-05-10), the legacy ``.appimage`` / ``.dmg`` as fallbacks. Windows
    is picked by scripts/lib/download-launcher-asset.ps1."""
    assets: List[Dict[str, Any]] = [
        a for a in (release.get("assets") or []) if isinstance(a, dict)]
    if arch is None:
        try:
            arch = platform.machine().lower()
        except Exception:  # noqa: BLE001 — platform probing is best-effort
            arch = ""
    arch = (arch or "").lower()

    def pick(predicate) -> Optional[Dict[str, Any]]:
        for a in assets:
            if predicate((a.get("name") or "").lower()):
                return a
        return None

    picked = None
    if os_name == "linux":
        if arch in ("x86_64", "amd64", "x64"):
            picked = pick(lambda n: n.endswith(".zip") and (
                "linux-x64" in n or "linux_x64" in n or "linux" in n))
        if picked is None:
            picked = pick(lambda n: n.endswith(".zip") and "linux" in n)
        if picked is None:
            picked = pick(lambda n: n.endswith(".appimage"))
    elif os_name == "macos":
        if arch in ("arm64", "aarch64"):
            picked = pick(lambda n: n.endswith(".zip") and (
                "arm64" in n or "aarch64" in n or "macos" in n))
        if picked is None:
            picked = pick(lambda n: n.endswith(".zip") and "macos" in n)
        if picked is None:
            if arch in ("arm64", "aarch64"):
                picked = pick(lambda n: n.endswith(".dmg") and (
                    "arm64" in n or "aarch64" in n))
            if picked is None:
                picked = pick(lambda n: n.endswith(".dmg"))
    if picked is None:
        return None
    name = str(picked.get("name") or "")
    sidecar_name = name + SIDECAR_SUFFIX
    sidecar = next((a for a in assets if a.get("name") == sidecar_name), None)
    return {
        "url": str(picked.get("browser_download_url") or ""),
        "name": name,
        "sha256_url": str((sidecar or {}).get("browser_download_url") or ""),
    }


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
    p_pick = sub.add_parser("pick-asset", help="the POSIX release asset + its sidecar URL")
    p_pick.add_argument("--os", required=True, dest="os_name")
    p_pick.add_argument("--release-json", required=True,
                        help="FILE holding the release document")
    p_pick.add_argument("--arch", default=None, help="override platform.machine()")
    p_ver = sub.add_parser("verify-sha256", help="check an archive against its sidecar")
    p_ver.add_argument("--file", required=True)
    p_ver.add_argument("--sidecar", required=True)
    p_ver.add_argument("--name", default=None, help="the asset name the sidecar must name")
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    if args.cmd == "api-url":
        print(release_api_url(checkout_version(Path(args.root) if args.root else None)))
        return 0
    if args.cmd == "pick-asset":
        try:
            release = json.loads(Path(args.release_json).read_text(encoding="utf-8"))
            if not isinstance(release, dict):
                raise ValueError("release document is not a JSON object")
        except (OSError, ValueError) as exc:
            print(f"release document unreadable: {exc}", file=sys.stderr)
            return PICK_UNREADABLE
        found = pick_asset(release, args.os_name, args.arch)
        if found is None:
            return PICK_NO_ASSET
        print(found["url"])
        print(found["name"])
        print(found["sha256_url"])
        return PICK_OK
    if args.cmd == "verify-sha256":
        code, message = verify_sha256(Path(args.file), Path(args.sidecar), args.name)
        print(message, file=sys.stdout if code == VERIFY_OK else sys.stderr)
        return code
    print(next_step(args.outcome, yes=args.yes))
    return 0


if __name__ == "__main__":
    sys.exit(main())
