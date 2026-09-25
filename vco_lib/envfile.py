# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Line-level parsing for ``.env`` / ``.claude/env`` managed-block text.

**One concern, one home** (v0.2.84 fix-pass): two readers had grown a
byte-identical copy of the SAME line-oriented env parse — split on universal
newlines, strip whitespace, drop an optional ``export`` prefix, skip
comments/blanks, ``partition("=")`` on the first ``=``, and strip one matching
pair of single/double quotes. This module owns that PARSING concern; each caller
keeps its own POLICY (managed-block scope vs whole-file, key filter) on top of
the shared ``(key, value)`` stream.

The parse rule is deliberately identical to the shell/PowerShell resolvers it
mirrors (``vct_secrets_resolve.sh::dotenv_get`` /
``vct_secrets_resolve.ps1::Get-DotenvValue``): line-oriented, ``KEY=VALUE`` or
``export KEY=VALUE``, one matching quote pair stripped, NO variable expansion,
NO command substitution, first match wins. It is CRLF-safe: ``str.splitlines()``
splits on universal newlines so a Windows ``\\r\\n``-terminated file parses
identically to a ``\\n`` one, and the per-line ``.strip()`` removes any residual
``\\r``.

Values are never logged by this module — callers decide what (if anything) to
do with them.

Reconciled callers:
  * ``vco_lib.install_weaviate._managed_env_value`` — managed-block-scoped key
    lookup for the dev-collection reference check.
  * ``vco_lib.config_projection._read_managed_env_canonical_value`` — the
    projection's own read-back of the ``.claude/env`` managed block (repoint
    audit, ``vco_lib.env_projection_check``). Until v0.2.97 it was a SECOND
    managed-block parser that disagreed with this one on ``\"``: the writer
    escapes ``"`` as ``\"`` inside a double-quoted value, the projection's
    reader reversed that and this module did not. The one rule now: a value
    read from the MANAGED BLOCK (markers given) has the writer's escape
    reversed; a whole-file dotenv read (no markers) does not, matching the
    shell/PowerShell dotenv resolvers.
  * ``vco_lib.knowledge_residue.project_env_value`` and
    ``install.py::_read_project_name_from_envfile`` — whole-file lookups.
  * ``vco_lib.agent_secrets._parse_dotenv_value`` — whole-file (no managed
    block) dotenv key lookup, tier-3 secret resolution.

NOT reconciled (deliberately): ``vco_lib.config_projection.retained_secret_keys_in``
(the pre-v0.2.73 secret-value detector, moved there from ``project_init`` in
v0.2.97) scans the managed block with a start-anchored regex whose contract
differs on edge cases (REQUIRES the literal ``export`` keyword, matches ONLY a
double-quoted value, tolerates trailing content after the closing quote).
Routing it through this generic parser would change edge behavior for
hand-edited managed blocks, so it keeps its own regex policy
(``config_projection._MANAGED_EXPORT_RE``).
"""
from __future__ import annotations

from typing import Iterator, Optional, Tuple

__all__ = [
    "extract_managed_block",
    "parse_env_line",
    "parse_env_lines",
    "parse_managed_env_lines",
    "env_value",
]


def _strip_one_quote_pair(value: str, *, writer_escapes: bool = False) -> str:
    """Strip a single matching pair of leading/trailing single OR double
    quotes. Matches the shell/PowerShell dotenv resolvers exactly — only ONE
    pair, only when the first and last char are the same quote char.

    ``writer_escapes`` (managed-block reads only): inside a double-quoted
    value, reverse the ONE escape VCO's block writer applies
    (``config_projection._build_managed_block``: ``"`` → ``\"``)."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        inner = value[1:-1]
        if writer_escapes and value[0] == '"':
            inner = inner.replace('\\"', '"')
        return inner
    return value


def extract_managed_block(
    text: str, begin_marker: str, end_marker: str,
) -> Optional[str]:
    """Return the substring from ``begin_marker`` up to ``end_marker``, or
    ``None`` when the block is absent / malformed (end before begin, either
    marker missing/empty). The returned slice INCLUDES the begin marker line and
    EXCLUDES everything at/after the end marker — the same ``text[begin:end]``
    slice the managed-block callers historically used, so the marker lines
    themselves (comments) are harmlessly skipped by :func:`parse_env_lines`.

    Scoping to the managed region ensures a user's own out-of-block export can
    never be read as a VCO-managed value.
    """
    if not begin_marker or not end_marker:
        return None
    begin = text.find(begin_marker)
    if begin == -1:
        return None
    # The END that closes THIS block: the first one after BEGIN.
    end = text.find(end_marker, begin + len(begin_marker))
    if end == -1:
        return None
    return text[begin:end]


def parse_env_lines(text: str, *, writer_escapes: bool = False) -> Iterator[Tuple[str, str]]:
    """Yield ``(key, value)`` for each assignment line in ``text``.

    Line rule (CRLF-safe): split on universal newlines; strip each line; drop an
    optional ``export `` prefix; skip blank lines, ``#`` comments, and lines with
    no ``=``; ``partition("=")`` on the FIRST ``=``; strip the key's surrounding
    whitespace; strip one matching quote pair from the value. Keys are yielded in
    file order (first occurrence first) — callers that want "first match wins"
    take the first yielded pair for a given key.
    """
    for line in text.splitlines():  # universal-newline split → CRLF-safe
        pair = parse_env_line(line, writer_escapes=writer_escapes)
        if pair is not None:
            yield pair


def parse_env_line(line: str, *, writer_escapes: bool = False) -> Optional[Tuple[str, str]]:
    """``(key, value)`` of ONE line under the :func:`parse_env_lines` rule, or
    ``None`` for a blank line, a ``#`` comment or a line with no ``=``.

    The single line grammar: callers that must act on individual lines (the
    unregister's per-occurrence secret strip, v0.2.97) parse with this, never a
    copy of it.
    """
    s = line.strip()  # trailing \r stripped here too
    if s.startswith("export "):
        s = s[len("export "):].lstrip()
    if not s or s.startswith("#") or "=" not in s:
        return None
    k, _, v = s.partition("=")
    return k.strip(), _strip_one_quote_pair(v.strip(), writer_escapes=writer_escapes)


def parse_managed_env_lines(
    text: str,
    *,
    begin_marker: Optional[str] = None,
    end_marker: Optional[str] = None,
) -> Iterator[Tuple[str, str]]:
    """Yield ``(key, value)`` assignment pairs from ``text``.

    When BOTH ``begin_marker`` and ``end_marker`` are supplied, parsing is
    scoped to the managed block (via :func:`extract_managed_block`); if the
    block is absent, NOTHING is yielded (there is no VCO-managed region to
    read). When the markers are omitted (e.g. a bare ``.env`` that has no
    managed block), the WHOLE text is parsed.

    This is the single line-level parser the reconciled key-lookup callers
    share; each layers its own policy (scope, key filter) on the yielded stream.
    """
    if begin_marker is not None and end_marker is not None:
        block = extract_managed_block(text, begin_marker, end_marker)
        if block is None:
            return
        # The managed block is written by ONE writer; reverse its escape.
        yield from parse_env_lines(block, writer_escapes=True)
        return
    yield from parse_env_lines(text)


def env_value(
    text: str,
    key: str,
    *,
    begin_marker: Optional[str] = None,
    end_marker: Optional[str] = None,
) -> Optional[str]:
    """First-match-wins lookup of ``key`` over :func:`parse_managed_env_lines`.

    Returns the (quote-stripped) value of the first matching assignment, or
    ``None`` when the key is absent (or the managed block, when requested, is
    missing). Convenience wrapper for the two key-lookup callers.
    """
    for k, v in parse_managed_env_lines(
        text, begin_marker=begin_marker, end_marker=end_marker,
    ):
        if k == key:
            return v
    return None
