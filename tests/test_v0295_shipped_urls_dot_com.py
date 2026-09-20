# SPDX-License-Identifier: AGPL-3.0-or-later
# Part of VibeCoded Orchestrator.
"""v0.2.95 lane F6 — no user-facing shipped URL still uses the .it host.

The product website consolidated on ``vibecodedtools.com`` (2026-09-16);
the old ``vibecodedtools.it`` host now answers 301, path-preserving, to
the ``.com`` host. Every URL a shipped artefact SHOWS to a user or
FETCHES at runtime must name the ``.com`` host directly (live-probed
2026-09-16: the ``.com`` apex, ``/privacy`` and ``/quickstart`` all serve
200 un-prefixed), so users are never one redirect away from content and
never see a mixed-host brand.

This is a source-text ratchet: the thing under test IS the repository's
own text, so a source scan is the correct instrument (same reasoning as
``tests/test_v0291_no_nul_bytes_in_sources.py``).

ALLOWLIST — every entry is a deliberate exception, recorded here so a
future removal is a conscious act, not drift:

- ``vibecodedtools.it/account`` — no page exists on EITHER host (live
  probe: ``.com/account`` and every locale variant 404). Replacing a 404
  with another 404 was ruled out; the runtime/licensing strings stay on
  ``.it`` so the moment the site grows an ``/account`` page the existing
  301 delivers it. Tracked in the lane report's "needs a page" list.
- ``vibecodedtools.it/modules/rl-reranker`` — same: 404 on both hosts.
- ``https://vibecodedtools.it/schemas/`` — JSON-Schema ``$schema``
  identifiers in ``launcher/bundled_manifests/``. Identifiers are
  versioned names, not fetchable links; changing one is a versioning
  decision reserved to the owner.
- ``schemas.vibecodedtools.it/`` — identifier-shaped fixture field
  (``update_endpoint_response_shape``); same reservation.
- ``api.vibecodedtools.it`` — historical comments only: that DNS record
  never existed and the default was retired 2026-05-06 in favour of the
  Supabase function URL (see ``VCThelpers/license/validator.py``).
- ``security@vibecodedtools.it`` — an email address, not a URL; both
  domains carry identical IONOS MX records (verified 2026-09-16), so the
  mailbox delivers as-is.
- the quoted, retired URL-secrecy guard phrase kept as history in
  ``licensing.rs`` and ``tests/test_license_validator.py`` — it appears
  both on one line ("must contain vibecodedtools.it") and wrapped across
  two (``vibecodedtools.it" / "must not contain supabase.co"``), so both
  spellings are allowed.
- a backticked bare ``vibecodedtools.it`` — the host named in prose
  inside the DNS-history comments (``validator.py``, ``licensing.rs``).

SCOPE: git-tracked files only (``git grep``). Untracked working-tree
files are invisible to CI by definition and, on a shared tree, belong to
other lanes mid-edit; the moment a file is committed it becomes tracked
and falls under this gate. ``CHANGELOG.md`` is skipped as a historical
record, and this test file is skipped because its own text must name the
forbidden string.

RED-PROOF: written BEFORE the source fix, this test failed on the
pre-fix tree naming ``README.md:307``, ``docs/TROUBLESHOOTING.md:951``,
``VCThelpers/telemetry/consent.py:51`` and
``templates/skills/gui-test/SKILL.md:36``; it passed once those four
user-facing strings were repointed at 200-proven ``.com`` URLs.
"""

from __future__ import annotations

import subprocess
import unittest

NEEDLE = "vibecodedtools.it"

# Ordered allow-markers; each is matched against the whole LINE containing
# an occurrence. Keep in sync with the module docstring above.
ALLOWED_LINE_MARKERS = (
    "vibecodedtools.it/account",
    "vibecodedtools.it/modules/rl-reranker",
    "https://vibecodedtools.it/schemas/",
    "schemas.vibecodedtools.it/",
    "api.vibecodedtools.it",
    "security@vibecodedtools.it",
    "must contain vibecodedtools.it",
    'vibecodedtools.it" / "must not contain supabase.co"',
    "`vibecodedtools.it`",
)

SKIPPED_PATHS = (
    "CHANGELOG.md",
    "tests/test_v0295_shipped_urls_dot_com.py",
)


def _grep_offender_candidates() -> list[str]:
    """``git grep`` the working tree of tracked files for the needle.

    ``-I`` skips binary files; ``git grep`` sees only tracked paths, which
    is the scope decision documented in the module docstring. Exit code 1
    means "no matches" (clean), not failure.
    """
    proc = subprocess.run(
        ["git", "grep", "-nI", "--fixed-string", NEEDLE, "--"]
        + [f":(exclude){path}" for path in SKIPPED_PATHS],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode not in (0, 1):
        raise AssertionError(f"git grep failed: {proc.stderr.strip()}")
    return [line for line in proc.stdout.splitlines() if line.strip()]


class NoDotItUserFacingUrls(unittest.TestCase):
    def test_no_vibecodedtools_it_outside_allowlist(self) -> None:
        offenders = [
            line
            for line in _grep_offender_candidates()
            # "path:lineno:content" — the marker check runs on the full line.
            if not any(marker in line for marker in ALLOWED_LINE_MARKERS)
        ]
        self.assertEqual(
            offenders,
            [],
            "vibecodedtools.it used outside the allowlist (the .com "
            "consolidation ratchet). Each hit is either a user-facing/"
            "runtime-fetched URL that must move to vibecodedtools.com, or a "
            "deliberate NEW exception that must be added to "
            "ALLOWED_LINE_MARKERS with a reason in this module's "
            "docstring:\n" + "\n".join(offenders),
        )


if __name__ == "__main__":
    unittest.main()
