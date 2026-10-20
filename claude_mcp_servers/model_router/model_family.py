# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Model-id structure: which ids are the same MODEL at different versions.

Two owner requirements (2026-09-10/16) rest on that question and neither can
be answered from an id string treated as opaque:

* **the picker shows only the LATEST version of each family** — Fable 5.1, not
  Fable 5 as well — so something has to know that ``claude-fable-5-1`` and
  ``claude-fable-5`` are one family two versions apart;
* **a NEW model inherits at least the window of the previous model in its
  family** until someone verifies its real one, so a model that ships tomorrow
  is not advertised at the client's conservative default merely because nobody
  has edited a table yet.

**Why this is parsed rather than tabulated.** The obvious alternative is a
hand-maintained map of families to ids. It goes stale on the day a model
ships, which is precisely the day both requirements above matter — the whole
point is that a model nobody has heard of yet lands in the picker correctly.
A structural rule cannot go stale that way: it is wrong only for an id shaped
unlike every id either upstream has ever returned, and that case resolves to
``unknown``, which is the honest answer rather than a wrong one.

**One shape breaks that claim today, and the owner has deferred the fix to
0.2.97.** A FOUR-digit tail is read as a version, not a date (only an
eight-digit segment is a date — see the date rule below), so a vendor's dated
build ``<family>-0731`` parses as version ``(731,)`` and outranks the newer
``<family>.1`` at ``(1,)`` under the latest-only filter: the snapshot is
published and the current model hidden. The shipped data hit this once
(``deepseek-v4-flash-0731``) and it is shielded for that ONE family by the
qwen row's ``catalog_exclude_prefixes``; any future vendor snapshot reproduces
it until the parser learns to read a valid ``MMDD`` tail as a date. So for
dated four-digit ids the rule above IS stale-able, and per-vendor exclusion is
the only mechanism holding.

The rule, in full
-----------------
Strip Claude Code's ``[1m]`` suffix and the vendor namespace, then split what
is left on ``-``, ``.`` and ``/``, then classify each segment BY SHAPE:

* an all-digit segment of exactly **8** digits is the release ``date``;
* any other all-digit segment joins ``version``, in order;
* every remaining segment joins ``family``, hyphen-separated, in order.

Eight digits specifically because that is ``YYYYMMDD``, the shape of the dated
first-party ids (``claude-3-7-sonnet-20250219``), and no version segment any
upstream publishes is eight digits long. Reading a date as a version component
would make a dated id the newest thing in its family by twenty million
points — which is the exact mistake that would hide every undated sibling.

Classifying by shape rather than by position is what makes
``claude-3-7-sonnet-20250219`` and ``claude-sonnet-4-6`` both resolve to
family ``claude-sonnet``: the version moved from the middle of the name to the
end between those two generations, and a positional rule would have made them
two unrelated families.

**Family names are scoped by their CATALOG family, not by this module.** Two
vendors may both publish a ``glm``; nothing here can tell them apart, because
the id alone does not say. Callers group by ``(family_id, parts.family)`` —
:mod:`model_router.catalog` does — so one vendor's versions can never be
compared against another's.

Vendor-neutral by construction: the namespace is removed through
:func:`model_router.routing.split_namespace` against the registry, and the
``[1m]`` suffix through :func:`model_router.routing.strip_1m`, so neither
spelling exists twice in the package.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping, Optional

from .routing import split_namespace, strip_1m
from .vendors import VENDORS, Vendor

#: The separators an upstream uses inside a model id. Data, not a literal in a
#: regex, so the set is readable and the pattern below cannot drift from it.
SEGMENT_SEPARATORS = ("-", ".", "/")

_SEGMENT_RE = re.compile(f"[{re.escape(''.join(SEGMENT_SEPARATORS))}]")

#: Length of an all-digit segment that is a ``YYYYMMDD`` release date rather
#: than a version component.
DATE_SEGMENT_DIGITS = 8


@dataclass(frozen=True)
class ModelIdParts:
    """One model id, taken apart.

    Attributes:
        family: the id with its version and date removed, hyphen-joined —
            ``claude-fable``, ``glm``, ``glm-flash``. Compare it only WITHIN
            one catalog family (see the module docstring).
        version: the numeric version segments, in the order they appeared.
            Tuples compare the way versions should: ``(5,) < (5, 3)``.
            Empty when the id carries no number at all.
        date: the ``YYYYMMDD`` segment, or ``None``. A string, not a date
            object: it is only ever compared to another one of the same shape,
            where lexicographic order IS chronological order, and parsing it
            would invent a failure mode for an id that is merely unusual.
        one_m: the id carried Claude Code's ``[1m]`` suffix.
        bare_id: the id with the suffix and the vendor namespace removed —
            the spelling an upstream and the context table both key on.
    """

    family: str
    version: tuple[int, ...]
    date: Optional[str]
    one_m: bool
    bare_id: str


def is_older_sibling(candidate: ModelIdParts, target: ModelIdParts) -> bool:
    """Is ``candidate`` a STRICTLY OLDER version of ``target``'s own model?

    The inheritance rule, in one place, because two callers make the same
    decision from different inputs: :func:`model_router.catalog._family_floor`
    (over one upstream's live rows) and
    :meth:`model_router.context_table.ContextTable.assume_window` (over the
    context table's rows). Two copies of "same family, older version" would
    drift, and the drift would be invisible — both sides would still answer,
    just differently, about which window a brand-new model inherits.

    Two conditions, each load-bearing:

    * **same family**, by the parsed stem — so ``claude-haiku-5`` can never
      inherit from ``claude-sonnet-5``, and ``glm-5.3-flash`` (family
      ``glm-flash``) never from ``glm-5.3`` (family ``glm``). An EMPTY family
      matches nothing, itself included: an id with no alphabetic segment tells
      us nothing about kinship, and pooling all of them would be a guess
      dressed as a rule.
    * **strictly older**, by the numeric version tuple — inheritance runs
      newer-from-older and never the reverse. A 200K model that ships after a
      1M one must not drag the 1M row down, and a 1M model must not lend its
      window BACKWARDS to an older model that never had it.

    Version ties are not "older", so two spellings of one release (dated and
    undated) never inherit from each other.
    """
    if not target.family or candidate.family != target.family:
        return False
    return candidate.version < target.version


def parse_model_id(
    model_id: str,
    vendors: Mapping[str, Vendor] = VENDORS,
) -> ModelIdParts:
    """Take ``model_id`` apart. Never raises; an unparseable id is ``unknown``.

    A blank id, an id of pure punctuation, an id with no digits — each yields
    parts that simply say so (empty family, empty version). Callers treat that
    as "nothing can be concluded", which is correct, and is why this has no
    error path: there is no id whose SHAPE should be able to fail a catalog
    fetch.
    """
    raw = (model_id or "").strip()
    bare_with_namespace = strip_1m(raw)
    one_m = bare_with_namespace != raw
    _vendor, bare_id = split_namespace(bare_with_namespace, vendors)

    family_parts: list[str] = []
    version: list[int] = []
    date: Optional[str] = None
    for segment in _SEGMENT_RE.split(bare_id):
        if not segment:
            continue
        # ``isascii`` guards ``str.isdigit``, which is True for digits in other
        # scripts too. Those join the family name instead, which is the
        # conservative outcome: a family that never matches beats a version
        # number read out of a character no upstream meant as one.
        if segment.isascii() and segment.isdigit():
            if len(segment) == DATE_SEGMENT_DIGITS:
                date = segment
            else:
                version.append(int(segment))
            continue
        family_parts.append(segment)

    return ModelIdParts(
        family="-".join(family_parts),
        version=tuple(version),
        date=date,
        one_m=one_m,
        bare_id=bare_id,
    )


__all__ = [
    "DATE_SEGMENT_DIGITS",
    "SEGMENT_SEPARATORS",
    "ModelIdParts",
    "is_older_sibling",
    "parse_model_id",
]
