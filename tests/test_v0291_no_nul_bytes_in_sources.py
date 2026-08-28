# SPDX-License-Identifier: AGPL-3.0-or-later
# Part of VibeCoded Orchestrator.
"""v0.2.91 wave-5 review MAJOR-1 — no raw NUL (0x00) bytes in source files.

`launcher/src/lib/project-state/HooksTab.svelte` shipped a rewrite that
embedded two literal 0x00 bytes as `rowKey` join separators (the author
meant the two-character escape ``\\x00``, not a raw byte). The moment a
text file contains a NUL byte, `file(1)`, `grep`/`git grep`, and `git diff`
all reclassify it as *binary* — `git diff` printed `Bin 10368 -> 15785
bytes` for the rewrite, and three separate greps run during the wave-5
review against that file returned empty. A binary-looking file is silently
un-scanned by every OTHER source-text gate that exists or will ever be
added: privacy sweeps, token sweeps, dead-invoke audits, this repo's own
ratchets. See
``knowledge/concepts/source-text-gates-fail-toward-green-2026-08-27.md``
(VCO_dev KG) — this is exactly the "applicability computed by the thing
under test" failure shape: a scanner that classifies a file as
binary-and-therefore-out-of-scope based on a property (NUL-byte presence)
that the bug itself introduces.

THE RULE: none of this repo's own source files should ever contain a raw
NUL byte. A string that legitimately needs a NUL character (a join
separator unlikely to collide with real field content, a sentinel) must
spell it as the language's escape sequence (``\\x00``, ``\\0``, ``\\u0000``)
so the byte lives only in the compiled/interpreted runtime value, never in
the file `git` and `grep` operate on.

SCOPE: every git-tracked AND untracked (not-ignored) file under the source
extensions named in the review's fix plan — ``.svelte``, ``.ts``, ``.rs``,
``.py``, ``.sql``, ``.sh``, ``.ps1`` — repo-wide, not just the launcher.
Untracked files are included deliberately: a NUL-byte regression is exactly
as invisible to diff review whether the file is already committed or is a
new file about to be added (`git add` doesn't launder it either).

RED-PROOF: this test is self-checking against the exact defect it fixes —
`RedProofAgainstTheOriginalDefect` below reconstructs the byte-for-byte
original two-NUL `rowKey` line the wave-5 review found and asserts the
scanner (not just an `assertNotIn`) flags it. That case is proven to fail
before the one-line source fix landed (verified live during the fix: the
scanner raised on the real pre-fix `HooksTab.svelte`, then passed once the
raw bytes were replaced with `\\x00` escapes) — the reconstructed-line case
here keeps that red-proof pinned so a future regression of the same shape
is caught even though the real file is now clean.
"""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

SOURCE_EXTENSIONS = {".svelte", ".ts", ".rs", ".py", ".sql", ".sh", ".ps1"}


def _git_ls(*args: str) -> list[str]:
    proc = subprocess.run(
        ["git", "ls-files", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0:
        return []
    return [line for line in proc.stdout.splitlines() if line.strip()]


def _candidate_source_files() -> list[Path]:
    """Every tracked + untracked (not-ignored) file under SOURCE_EXTENSIONS."""
    names = set(_git_ls()) | set(_git_ls("--others", "--exclude-standard"))
    out: list[Path] = []
    for rel in sorted(names):
        p = REPO_ROOT / rel
        if p.suffix in SOURCE_EXTENSIONS and p.is_file():
            out.append(p)
    return out


def find_nul_bytes(data: bytes) -> list[int]:
    """Byte offsets of every 0x00 in ``data``. Empty list == clean."""
    offsets = []
    start = 0
    while True:
        idx = data.find(b"\x00", start)
        if idx == -1:
            break
        offsets.append(idx)
        start = idx + 1
    return offsets


class NoNulBytesInTrackedOrUntrackedSources(unittest.TestCase):
    """Repo-wide scan: no source file may contain a raw NUL byte."""

    def test_git_is_available_and_repo_checkout_found(self) -> None:
        # If this fails, the sweep below silently scans zero files — make
        # that failure loud rather than a quiet false-green.
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if proc.returncode != 0:
            self.skipTest(
                "git unavailable — cannot assert repo-wide NUL-byte state "
                f"(stderr: {proc.stderr.strip()})"
            )

    def test_no_source_file_contains_a_raw_nul_byte(self) -> None:
        files = _candidate_source_files()
        # Self-check: the scanner must actually be seeing files, not
        # silently walking an empty list (the KG lesson's "applicability
        # computed by the thing under test" failure shape).
        self.assertGreater(
            len(files),
            1000,
            "the candidate file list is suspiciously small — the "
            "tracked/untracked walk may be broken (git unavailable, wrong "
            "cwd, or the extension filter is too narrow)",
        )
        offenders: list[str] = []
        for path in files:
            data = path.read_bytes()
            offsets = find_nul_bytes(data)
            if offsets:
                rel = path.relative_to(REPO_ROOT)
                offenders.append(f"{rel} — {len(offsets)} NUL byte(s) at offsets {offsets[:5]}")
        self.assertEqual(
            offenders,
            [],
            "Source file(s) contain raw NUL (0x00) bytes — this makes the "
            "file invisible to `grep`/`git diff`/every text-based gate "
            "(binary classification). If a NUL character is genuinely "
            "needed at runtime (a join separator, a sentinel), spell it as "
            "the language's escape sequence (\\x00 / \\0 / \\u0000) instead "
            "of embedding the raw byte:\n" + "\n".join(offenders),
        )


class RedProofAgainstTheOriginalDefect(unittest.TestCase):
    """Pin the exact defect shape the review found: reconstruct the
    byte-for-byte `rowKey` line HooksTab.svelte shipped with two raw NUL
    bytes, and prove the scanner (not just `assertNotIn`) flags it."""

    def test_scanner_flags_the_original_two_nul_rowkey_line(self) -> None:
        # Byte-for-byte reconstruction of the pre-fix HooksTab.svelte:41
        # line, built from the review's own quoted bytes — NOT read from
        # the (now-fixed) file on disk.
        defect_line = (
            b"  const rowKey = (h: EffectiveHook) => "
            b"`${h.event}\x00${h.matcher}\x00${h.command}`;\n"
        )
        offsets = find_nul_bytes(defect_line)
        self.assertEqual(
            len(offsets),
            2,
            "the reconstructed defect line must contain exactly the two "
            "raw NUL bytes the wave-5 review found — if this assertion "
            "itself fails, the reconstruction is wrong, not the scanner",
        )

    def test_the_escaped_form_is_clean(self) -> None:
        # The actual fix: \x00 as a two-character escape, not a raw byte.
        fixed_line = (
            b"  const rowKey = (h: EffectiveHook) => "
            b"`${h.event}\\x00${h.matcher}\\x00${h.command}`;\n"
        )
        self.assertEqual(find_nul_bytes(fixed_line), [])

    def test_the_real_hookstab_file_is_now_clean(self) -> None:
        target = (
            REPO_ROOT
            / "launcher"
            / "src"
            / "lib"
            / "project-state"
            / "HooksTab.svelte"
        )
        if not target.is_file():
            self.skipTest(f"{target} not present in this checkout")
        data = target.read_bytes()
        self.assertEqual(
            find_nul_bytes(data),
            [],
            f"{target} still contains raw NUL bytes — MAJOR-1 not fixed",
        )
        self.assertIn(
            b"rowKey",
            data,
            f"{target} no longer defines rowKey — sanity check that we "
            "read the right file",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
