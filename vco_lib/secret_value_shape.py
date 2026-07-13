# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Single-line secret-value shape SSOT (v0.2.80 Part A).

A secret file in the file store (``~/.vct-secrets/{shared,projects/<NAME>}/<key>``)
is meant to hold ONE secret. When a MULTI-LINE BLOB — several secrets glued
together (a classic PAT on line 0 plus ``KEY=value`` continuation lines) — ends
up in such a file, the resolver reads the whole file and hands a multi-line
"password" to ``git push`` / ``gh``, which fails auth silently. This module is
the preventive layer: a precise classifier that a value can be shape-checked
against BEFORE it is written to any store, so a blob is rejected at the write
boundary instead of being faithfully propagated.

Source of truth (A > B > C)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Python is the SSOT for the value-shape predicate. Two other languages MUST
mirror it exactly:

* bash — ``tools/vct-secrets/vct::_is_single_line_secret`` (pure-bash, runs from
  ``~/.vct-secrets/vct`` in git-credential-helper context with no venv /
  PYTHONPATH guarantee, so it cannot ``python -m`` this module — it re-implements
  the predicate and is locked to it by the parity fixture).
* Rust — ``launcher/src-tauri/src/commands/secrets_import.rs`` (the launcher's
  in-process import path).

All three are pinned to the SAME canonical fixture
``tests/fixtures/secret_value_shape_parity.json``. A divergence in any mirror
fails that language's parity test. When you change the predicate here, update
the fixture AND both mirrors in the same change.

The precision of the predicate is the PRIMARY mechanism (USER ruling 2026-07-13):
a legitimate multi-line secret (a PEM / OpenSSH private key: ``-----BEGIN … KEY
-----`` + base64 body + ``-----END … KEY-----``) is structurally distinct from a
blob (column-0 ``KEY=`` / ``export KEY=`` continuation lines), and the classifier
ACCEPTS the PEM without needing any escape-hatch flag. The ``--allow-multiline``
opt-in on ``vct set`` (surfaced here via the ``allow_multiline`` parameter) is a
SECONDARY escape hatch for a multi-line format the allowlist does not yet
recognise — never the thing that makes a PEM work.

Public API
~~~~~~~~~~

* :func:`is_single_line_secret` — ``(ok, reason)``. ``ok=True`` (reason ``""``)
  for a single-line value OR an allowlisted legit multi-line secret. ``ok=False``
  with a machine-stable ``reason`` slug for a blob-shaped value.
* :func:`classify_secret_value` — a taxonomy tag (``"ok"`` / ``"legit_multiline"``
  / ``"blob"`` / ``"length_corruption"``) used by the doctor + recovery paths to
  branch on the corruption mode.

No value is ever printed or logged by this module. It operates purely on the
string it is handed and returns a verdict.
"""

from __future__ import annotations

import re
from typing import List, Tuple

# ---------------------------------------------------------------------------
# Named constants — the mirrors (bash / Rust) reproduce these EXACTLY.
# ---------------------------------------------------------------------------

#: A ``github_pat``-named single-line value at or above this length is almost
#: certainly a concatenated / duplicated write (documented in the shipped
#: template ``templates/vct-secrets-shared-readme.template``). A well-formed
#: classic PAT is 40 chars; a fine-grained PAT is ~93. 200 is a wide margin
#: that no legitimate GitHub token reaches.
GITHUB_PAT_MAX_LEN = 200

#: The blob signature. A post-first-line that matches this at COLUMN 0 is an
#: env-assignment / ``export KEY=`` continuation — the fingerprint of a blob
#: (multiple secrets concatenated). The column-0 anchor + env-key charset is
#: what excludes indented JSON/YAML continuations (leading whitespace) and PEM
#: base64 body lines (no ``WORD=`` at column 0).
_BLOB_KEY_EQ_RE = re.compile(r"^(?:export[ \t]+)?[A-Za-z_][A-Za-z0-9_]*=")

#: Legit multi-line allowlist. Each entry is a ``(begin_re, end_re)`` pair; a
#: value is accepted as ``legit_multiline`` when its first non-empty line
#: matches ``begin_re`` AND its last non-empty line matches ``end_re``. Named
#: as a list so future formats (e.g. PGP blocks) extend it without touching the
#: predicate logic.
_LEGIT_MULTILINE_ALLOWLIST: List[Tuple[re.Pattern, re.Pattern]] = [
    (
        re.compile(r"^-----BEGIN [A-Z0-9 ]*(?:PRIVATE KEY|CERTIFICATE|PUBLIC KEY)-----$"),
        re.compile(r"^-----END [A-Z0-9 ]*(?:PRIVATE KEY|CERTIFICATE|PUBLIC KEY)-----$"),
    ),
]

#: A GitHub classic PAT: ``ghp_`` + exactly 36 base62 chars → 40 total.
_CLASSIC_PAT_RE = re.compile(r"^ghp_[A-Za-z0-9]{36}$")

#: Prefixes that identify a value as a github-pat-shaped token even when the
#: caller does not pass a ``key_name``. Used by :func:`classify_secret_value`
#: to flag a single-line ``ghp_``-prefixed token whose length is wrong as
#: ``length_corruption`` (distinct from a blob — there is nothing to split).
_GHP_PREFIX = "ghp_"

#: Control characters that must never appear in a secret value. ``\n`` and
#: ``\r`` are handled separately (they define line structure); this set is the
#: OTHER C0 controls plus DEL, which have no place in a token/key/password and
#: signal a corrupted or binary-contaminated value.
_FORBIDDEN_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

#: A ``github_pat``-style key name (the length heuristic only applies to these).
_GITHUB_PAT_KEY_RE = re.compile(r"^github_pat(?:[._-].*)?$", re.IGNORECASE)


def _non_empty_lines(value: str) -> List[str]:
    """Return the value's lines with empty / whitespace-only lines removed.

    Splits on both ``\\n`` and ``\\r`` boundaries (``str.splitlines`` handles
    ``\\r\\n``, ``\\n`` and lone ``\\r`` uniformly). Trailing whitespace on each
    line is stripped so a PEM whose lines have trailing spaces still matches the
    BEGIN/END anchors.
    """
    return [ln.rstrip() for ln in value.splitlines() if ln.strip()]


def _is_legit_multiline(value: str) -> bool:
    """True when ``value`` matches an allowlisted legit multi-line format.

    Currently the PEM / OpenSSH family: first non-empty line is a BEGIN marker
    and last non-empty line is the matching END marker. The body lines are NOT
    required to be validated as base64 — the BEGIN/END frame plus the blob
    signature check (no column-0 ``WORD=`` line) is sufficient discrimination,
    and refusing to parse the base64 body keeps the predicate cheap and
    format-tolerant (headers, blank lines inside armored blocks, etc.).
    """
    lines = _non_empty_lines(value)
    if len(lines) < 2:
        return False
    first, last = lines[0], lines[-1]
    for begin_re, end_re in _LEGIT_MULTILINE_ALLOWLIST:
        if begin_re.match(first) and end_re.match(last):
            return True
    return False


def _has_blob_signature(value: str) -> bool:
    """True when any post-first-line is a column-0 ``KEY=`` / ``export KEY=``.

    This is the blob fingerprint: a multi-secret concatenation where line 0 is a
    bare token and the following lines are ``KEY=value`` assignments. The
    column-0 anchor deliberately does NOT fire on indented continuations
    (leading whitespace → not column 0) or PEM base64 bodies (no ``WORD=``).
    """
    lines = value.splitlines()
    for line in lines[1:]:
        if _BLOB_KEY_EQ_RE.match(line):
            return True
    return False


def is_single_line_secret(
    value: str, *, allow_multiline: bool = False, key_name: str = ""
) -> Tuple[bool, str]:
    """Classify ``value`` as a writable single-well-formed secret vs a blob.

    Args:
        value: The raw secret value about to be written.
        allow_multiline: Escape hatch (``vct set --allow-multiline``). When
            ``True``, a multi-line value that is NOT an allowlisted legit format
            is accepted anyway (reason ``""``) — the caller explicitly vouches
            for it. Embedded control chars and the github_pat length heuristic
            are STILL rejected, because those signal corruption regardless of
            the caller's intent.
        key_name: The key the value is stored under (optional). When it names a
            ``github_pat``-style key, the length heuristic (>= ``GITHUB_PAT_MAX_LEN``
            chars → reject) applies. Key-name-agnostic when empty — a write
            boundary without a reliable key name still gets the structural
            (newline / blob-signature) checks, just not the length heuristic.

    Returns:
        ``(ok, reason)``:

        * ``(True, "")`` — a single-line value, an allowlisted legit multi-line
          secret (PEM/cert/OpenSSH), or (with ``allow_multiline``) a caller-
          vouched multi-line value.
        * ``(False, reason)`` — a blob-shaped value. ``reason`` is a machine-
          stable slug: ``"blob_key_eq_continuation"`` (column-0 ``KEY=`` line),
          ``"embedded_newline"`` (multi-line with no recognised structure),
          ``"control_char"`` (a forbidden control character), or
          ``"github_pat_over_200"`` (an over-long ``github_pat``-named value).

    This function is the SSOT the bash + Rust mirrors reproduce. Order of checks
    is significant and MUST match across all three implementations.
    """
    # A control char anywhere is corruption — reject even under allow_multiline,
    # even for a single line. (\n / \r are excluded from this class; they define
    # line structure and are handled by the multi-line logic below.)
    if _FORBIDDEN_CONTROL_RE.search(value):
        return (False, "control_char")

    is_github_pat_key = bool(key_name) and bool(
        _GITHUB_PAT_KEY_RE.match(key_name.strip())
    )

    # Strip trailing whitespace/newlines (a single trailing newline is normal on
    # a file-store secret and must not make an otherwise-single-line value look
    # multi-line). Interior \n / \r are what matter.
    trimmed = value.rstrip("\r\n").rstrip()
    if "\n" not in trimmed and "\r" not in trimmed:
        # Genuinely single-line. Apply the github_pat length heuristic: an
        # over-long single-line value under a github_pat-named key is a
        # concatenation (documented in the shared-readme template). The length
        # gate fires ONLY for github_pat-named keys — a legitimately long secret
        # under some other key name (a JWT, a long base64 API key) is not a blob
        # just because it is long. allow_multiline does NOT bypass this: an
        # over-long github_pat value is corruption regardless of caller intent.
        if is_github_pat_key and len(trimmed) >= GITHUB_PAT_MAX_LEN:
            return (False, "github_pat_over_200")
        return (True, "")

    # From here down the value is multi-line (has an interior \n or \r).
    if _is_legit_multiline(trimmed):
        return (True, "")

    if allow_multiline:
        # Caller explicitly vouches for an unrecognised multi-line format.
        return (True, "")

    if _has_blob_signature(trimmed):
        return (False, "blob_key_eq_continuation")

    # Multi-line, not a legit format, no column-0 KEY= line: still not a valid
    # single-line secret. Reject with the generic embedded-newline reason.
    return (False, "embedded_newline")


def classify_secret_value(value: str, key_name: str = "") -> str:
    """Return a corruption-taxonomy tag for ``value`` (Part C).

    Tags:

    * ``"ok"`` — a single well-formed value.
    * ``"legit_multiline"`` — an allowlisted legit multi-line secret
      (PEM/cert/OpenSSH). Never flagged.
    * ``"blob"`` — a multi-secret concatenation (multi-line, column-0 ``KEY=``
      lines, embedded newlines, or an over-long github_pat value).
      ``vct recover-blob`` can split it.
    * ``"length_corruption"`` — a SINGLE-LINE value that is a recognisably
      malformed token: a ``ghp_``-prefixed value whose length is not 40 and has
      no embedded newline (e.g. a truncated / over-padded classic PAT). This is
      DISTINCT from a blob — there is nothing to split; recovery is manual
      (re-issue or delete the token). ``key_name`` refines detection for
      github_pat-named keys.

    Args:
        value: The raw secret value.
        key_name: The key the value is stored under (optional). When it names a
            github_pat-style key, the length heuristics apply to a bare token
            even without a ``ghp_`` prefix.
    """
    ok, reason = is_single_line_secret(value, key_name=key_name)
    trimmed = value.rstrip("\r\n").rstrip()

    if ok:
        # Accepted by the predicate: a single-line value or a legit PEM.
        if _is_legit_multiline(trimmed):
            return "legit_multiline"

        # Single-line: flag a ghp_-prefixed token whose shape is wrong (not the
        # exact 40-char classic PAT) as length_corruption — malformed but
        # single-line, so there is nothing to split; recovery is manual. A
        # well-formed classic PAT (or any non-ghp_ single value) is "ok".
        if trimmed.startswith(_GHP_PREFIX) and not _CLASSIC_PAT_RE.match(trimmed):
            return "length_corruption"
        return "ok"

    # Rejected by the predicate.
    #  * github_pat_over_200 → single-line over-long github_pat: nothing to
    #    split, manual re-issue → length_corruption (NOT a splittable blob).
    #  * control_char / blob_key_eq_continuation / embedded_newline → the value
    #    is a multi-secret concatenation or otherwise malformed multi-line →
    #    blob (Part B recover-blob can split it, or at least back it up).
    if reason == "github_pat_over_200":
        return "length_corruption"
    return "blob"
