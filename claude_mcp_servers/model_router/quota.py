# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Turning a vendor's "you are out of quota" into something a user can act on.

The gateway's general relay policy is VERBATIM — a vendor's own error text is
almost always more useful than anything a proxy could invent. Quota exhaustion
is the one documented exception, and the incident that bought it reads like
this: a vendor returned ``429 [1310] Weekly/Monthly Limit Exhausted``, the
gateway relayed it unchanged, and the user — whose client was pointed at a
LOCAL endpoint and who had a Claude subscription sitting idle — read it as
their Claude limit and stopped working for the rest of the day. The bytes were
honest; the message was not, because the one fact that mattered (this was the
VENDOR's limit, and another route in the same picker is unaffected) was the
one fact the vendor's text could not contain.

So on a vendor route, and only for a quota status, the gateway substitutes its
own error naming the vendor and the way out. The upstream body is not thrown
away: it goes to the log at DEBUG, where a support question can still reach it.

**"Out of quota" and "going too fast" are different facts and get different
sentences.** Both arrive as HTTP 429, and the first version of this module
called every one of them exhaustion — which is the same class of confident
wrong answer the incident above was made of, only pointing the other way: a
per-minute limiter (Z.ai code ``1302``, a ``retry-after`` of thirty seconds)
told the user to abandon the model and switch families for something that
cleared itself while they read the message. :func:`classify_quota` therefore
demands POSITIVE evidence before it says "exhausted" and otherwise says the
honest, weaker thing. Both sentences keep the remedy and the "not billed to
Anthropic" clause: those are true either way, and they are what the incident
was about.

**The substitution is JSON even when the client asked for a stream.** An SSE
error frame looks like the more considerate answer and is the wrong one: the
Anthropic SDKs parse a non-2xx body as JSON regardless of what the request
asked for, so ``event: error\ndata: …`` reaches ``JSON.parse`` as
``event: error…`` and throws — the typed ``rate_limit_error`` is lost and the
user gets a parser exception instead of the sentence this module exists to
show them.

Nothing here names a vendor: the display name arrives from the registry row.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional

#: Statuses treated as "this subscription is out of quota". 429 is the
#: documented rate-limit status; 402 (Payment Required) is what a
#: pay-as-you-go account returns when its balance hits zero, which is the same
#: situation from the user's chair and has the same remedy.
QUOTA_STATUSES: tuple[int, ...] = (402, 429)

#: Status the CLIENT sees. Always 429, including for an upstream 402: Claude
#: Code renders ``rate_limit_error`` with a retry affordance, and 402 from a
#: local proxy invites the reading "my Claude account needs paying", which is
#: exactly the false conclusion this module exists to prevent.
CLIENT_QUOTA_STATUS = 429

#: Above this, a numeric reset hint is a UNIX timestamp rather than a delay.
#: (10**9 is 2001-09-09; no rate-limit window is a billion seconds long, so
#: the two readings cannot overlap in practice.) Without the distinction a
#: vendor that returns an absolute epoch renders as "resets in 1757300000s".
_EPOCH_THRESHOLD_S = 10**9

#: Headers carrying a reset hint, in the order they are trusted.
_RESET_HEADERS = (
    "retry-after",
    "x-ratelimit-reset",
    "ratelimit-reset",
    "x-ratelimit-reset-requests",
)

#: JSON keys carrying a reset hint, matched anywhere in the error document.
_RESET_KEYS = (
    "reset_at",
    "resets_at",
    "reset_time",
    "reset",
    "retry_after",
    "next_reset",
)

_MAX_DEPTH = 6

#: The two readings of a quota status. Strings rather than an enum because
#: they travel into a log line and a test assertion, and an enum's ``.value``
#: at every such site buys nothing here.
CLASS_EXHAUSTED = "exhausted"
CLASS_RATE_LIMITED = "rate_limited"

#: Substrings that are POSITIVE evidence of exhaustion, matched
#: case-insensitively against the vendor's body and its header VALUES.
#: PROSE only — a vendor's own error CODE is stronger evidence and is matched
#: a tier higher, in :func:`_mentions_exhaustion_code`, whichever form it
#: arrives in. Keeping ``[1310]`` here made the tier depend on the SHAPE of
#: the body rather than on what it said: the same code ranked first inside a
#: JSON ``code`` field and last as bracketed text, so
#: ``[1310] Weekly/Monthly Limit Exhausted`` with a 30-second ``retry-after``
#: came out rate-limited while ``{"code":"1310"}`` with the same header came
#: out exhausted.
_EXHAUSTION_MARKERS: tuple[str, ...] = (
    "limit exhausted",
    "quota exhausted",
    "quota exceeded",
    "exceeded your current quota",
    "insufficient_quota",
    "insufficient balance",
    "insufficient credits",
    "out of credits",
    "no remaining credits",
)

#: JSON keys that carry a vendor's own error code. Not ``type``: in an
#: Anthropic-shaped envelope that field holds the error CLASS
#: (``rate_limit_error``), so reading it as a code would be reading a
#: different thing that happens to sit next door.
_CODE_KEYS = ("code", "error_code")

#: Vendor error codes that MEAN exhaustion, as strings so ``"1310"`` and
#: ``1310`` compare the same. Only codes whose meaning is documented or was
#: observed live belong here: ``1310`` is Z.ai's "Weekly/Monthly Limit
#: Exhausted" (2026-09-08). Its neighbour ``1302`` is a per-minute rate limit
#: and is deliberately ABSENT — that pair is the whole reason this
#: classification exists.
_EXHAUSTION_CODES = frozenset({"1310"})

#: The same codes as they appear in PROSE — bracketed, the way a vendor
#: prints them when there is no JSON envelope
#: (``429 [1310] Weekly/Monthly Limit Exhausted``, verbatim from the
#: 2026-09-08 incident). Bracketed rather than bare so a ``1310`` inside an
#: unrelated number cannot trip it. Built from :data:`_EXHAUSTION_CODES` so
#: the two spellings of one fact cannot drift apart.
_EXHAUSTION_CODE_MARKERS: tuple[str, ...] = tuple(
    f"[{code}]" for code in sorted(_EXHAUSTION_CODES)
)

#: A reset this far away is not a rate-limit window. Ten minutes: the longest
#: per-minute/per-hour limiter still resets inside it, and anything beyond is
#: a daily/weekly/monthly allowance — i.e. exhaustion by another name. Used
#: only on a hint the vendor supplied; an ABSENT hint is not evidence of
#: anything and must not be read as exhaustion.
_LONG_RESET_S = 600

#: How much of a body is scanned for the markers. The body reaches this
#: module already bounded by the caller's peek; this second bound keeps the
#: lowercase copy small when a vendor answers with a page of HTML.
_SCAN_LIMIT_BYTES = 8192


def _render_hint(value: Any) -> Optional[str]:
    """A hint the user can read: seconds become ``in Ns``, dates stay as-is."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return _render_number(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.isdigit():
            return _render_number(int(text))
        return text[:64]
    return None


def _render_number(value: float) -> Optional[str]:
    """A delay renders as ``in Ns``; an absolute epoch renders as a time."""
    seconds = int(value)
    if seconds >= _EPOCH_THRESHOLD_S:
        try:
            return (
                datetime.fromtimestamp(seconds, timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
            )
        except (OverflowError, OSError, ValueError):
            return None
    return f"in {seconds}s"


def _find_reset_value(
    body: bytes, headers: Mapping[str, str] | None = None,
) -> Any:
    """The RAW reset value the vendor supplied — header first, then body.

    Split out from :func:`find_reset_hint` because two readers need the same
    search and only one of them wants it rendered: the message wants prose,
    :func:`classify_quota` wants a distance in seconds. One search, two
    consumers, so a vendor whose hint the renderer finds can never be a
    vendor whose hint the classifier misses.
    """
    if headers:
        lowered = {k.lower(): v for k, v in headers.items()}
        for name in _RESET_HEADERS:
            value = lowered.get(name)
            if value is not None and _render_hint(value):
                return value
    payload = _as_json(body)
    if payload is None:
        return None
    return _search_json(
        payload, _RESET_KEYS, lambda value: bool(_render_hint(value)),
    )


def _as_json(body: bytes) -> Any:
    """The body as a JSON document, or ``None``. Never raises."""
    if not body:
        return None
    try:
        return json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return None


def _search_json(
    node: Any,
    keys: tuple[str, ...],
    accept: "Callable[[Any], bool]",
    depth: int = 0,
) -> Any:
    """First value under any of ``keys``, anywhere, that ``accept`` likes.

    ONE traversal, parameterised, because two readers walk the same error
    document for different keys — the reset hint and the vendor's own error
    code — and two copies of a depth-bounded recursive walk is exactly the
    kind of near-duplicate that drifts. A value ``accept`` rejects is skipped
    rather than returned, so a key that is present but useless does not stop
    the search at a shallower node than the one that answers.
    """
    if depth > _MAX_DEPTH:
        return None
    if isinstance(node, dict):
        for key in keys:
            if key in node and accept(node[key]):
                return node[key]
        for value in node.values():
            found = _search_json(value, keys, accept, depth + 1)
            if found is not None:
                return found
        return None
    if isinstance(node, list):
        for item in node[:32]:
            found = _search_json(item, keys, accept, depth + 1)
            if found is not None:
                return found
    return None


def find_reset_hint(
    body: bytes, headers: Mapping[str, str] | None = None,
) -> Optional[str]:
    """Best-effort reset time from the vendor's headers, then its body.

    Best-effort in the strict sense used in this codebase: when nothing can be
    confirmed the answer is ``None`` and the message simply omits the clause.
    A guessed reset time would be worse than no reset time — the user would
    come back at the wrong hour and conclude the gateway lies.
    """
    return _render_hint(_find_reset_value(body, headers))


def _reset_distance_s(value: Any) -> Optional[float]:
    """Seconds from now until ``value``, or ``None`` when it cannot be read.

    Three shapes reach here: a delay in seconds, an absolute epoch, and an
    ISO-8601 timestamp. ``None`` means "unreadable", NEVER "soon": the
    classifier treats an unreadable hint as no evidence at all.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return _distance_from_number(float(value))
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.isdigit():
        return _distance_from_number(float(text))
    stamp = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (parsed - datetime.now(timezone.utc)).total_seconds()


def _distance_from_number(value: float) -> float:
    """A delay stays a delay; an epoch becomes the delay it implies."""
    if value >= _EPOCH_THRESHOLD_S:
        return value - datetime.now(timezone.utc).timestamp()
    return value


def _mentions_exhaustion_code(body: bytes) -> bool:
    """True when the vendor's OWN CODE says the allowance is used up.

    The strongest evidence there is, and the only one that outranks a
    readable reset time: a code is a vendor's own classification of its own
    refusal, where prose is a sentence that may be describing either thing.

    BOTH spellings count, and that is the point — a JSON ``code`` field and
    the bracketed form a vendor prints when it sends no envelope are the same
    claim. Ranking them differently made the classification depend on the
    body's shape rather than on what the vendor said.
    """
    scanned = body[:_SCAN_LIMIT_BYTES]
    text = scanned.decode("utf-8", "replace").lower()
    if any(marker in text for marker in _EXHAUSTION_CODE_MARKERS):
        return True
    payload = _as_json(scanned)
    if payload is None:
        return False
    return _search_json(payload, _CODE_KEYS, _is_exhaustion_code) is not None


def _mentions_exhaustion_words(
    body: bytes, headers: Mapping[str, str] | None = None,
) -> bool:
    """True when the vendor's own WORDS say the allowance is used up.

    Weaker than a code and weaker than a readable reset, because the same
    vocabulary describes both readings: "Quota exceeded: 60 requests per
    minute" is a rate limit that says "quota exceeded". So this is consulted
    only when there is no reset time to read — see :func:`classify_quota`.
    """
    haystack = body[:_SCAN_LIMIT_BYTES].decode("utf-8", "replace").lower()
    if headers:
        haystack += " " + " ".join(str(v).lower() for v in headers.values())
    return any(marker in haystack for marker in _EXHAUSTION_MARKERS)


def _is_exhaustion_code(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return str(int(value)) in _EXHAUSTION_CODES
    return isinstance(value, str) and value.strip() in _EXHAUSTION_CODES


def classify_quota(
    status: int,
    body: bytes = b"",
    headers: Mapping[str, str] | None = None,
) -> str:
    """:data:`CLASS_EXHAUSTED` only on POSITIVE evidence; else rate-limited.

    The evidence is ORDERED by how specific it is, strongest first, and the
    order is the whole design — reading prose before a reset time made
    "Quota exceeded: 60 requests per minute" with ``Retry-After: 30`` come
    out as exhaustion and send the user off to another model family for
    thirty seconds:

    1. **The vendor's own error CODE** (:data:`_EXHAUSTION_CODES`). A code is
       the vendor classifying its own refusal; nothing beats it.
    2. **HTTP 402.** Payment Required is a BALANCE condition, not a window —
       no amount of waiting clears it.
    3. **A READABLE reset time.** It answers the question directly: inside
       :data:`_LONG_RESET_S` it is a rate-limit window, beyond it an
       allowance. Either way this is a fact, not a reading of prose, so it
       decides — including deciding RATE_LIMITED.
    4. **The vendor's words** (:data:`_EXHAUSTION_MARKERS`), and only when
       there is no reset time to read, because the same vocabulary describes
       both situations.

    Everything else — a bare 429, an unparseable hint, no hint at all — is
    :data:`CLASS_RATE_LIMITED`. Absence of evidence is not evidence: "we do
    not know which of the two this is" is exactly what the weaker sentence
    says, and it is true in both cases.
    """
    if _mentions_exhaustion_code(body):
        return CLASS_EXHAUSTED
    if status == 402:
        return CLASS_EXHAUSTED
    distance = _reset_distance_s(_find_reset_value(body, headers))
    if distance is not None:
        return (
            CLASS_EXHAUSTED if distance > _LONG_RESET_S else CLASS_RATE_LIMITED
        )
    if _mentions_exhaustion_words(body, headers):
        return CLASS_EXHAUSTED
    return CLASS_RATE_LIMITED


def quota_message(
    vendor_display_name: str,
    reset_hint: Optional[str] = None,
    *,
    status: int,
    classification: str,
) -> str:
    """The user-facing sentence. Names the vendor, the remedy, and the bill.

    ``status`` is the UPSTREAM status, not the one the client will see: the
    substitution always answers 429 (see :data:`CLIENT_QUOTA_STATUS`), so
    without this the message could not tell a 402 from a 429 and a support
    question had nothing to work from. ``classification`` and ``status`` are
    keyword-only and have no defaults on purpose — a default here would let a
    caller claim exhaustion by omission, which is the defect this argument
    exists to remove.
    """
    resets = f", resets {reset_hint}" if reset_hint else ""
    if classification == CLASS_EXHAUSTED:
        headline = f"{vendor_display_name} quota exhausted"
        remedy = "pick a Claude model with /model"
    else:
        headline = f"{vendor_display_name} rate-limited or out of quota"
        remedy = "retry shortly, or pick a Claude model with /model"
    return (
        f"{headline} (HTTP {status}){resets} — {remedy} "
        "(this request was not billed to Anthropic)"
    )


def quota_error_body(
    vendor_display_name: str,
    reset_hint: Optional[str] = None,
    *,
    status: int,
    classification: str,
) -> dict:
    """Anthropic-shaped error envelope, so the client renders the text."""
    return {
        "type": "error",
        "error": {
            "type": "rate_limit_error",
            "message": quota_message(
                vendor_display_name,
                reset_hint,
                status=status,
                classification=classification,
            ),
        },
    }


__all__ = [
    "CLASS_EXHAUSTED",
    "CLASS_RATE_LIMITED",
    "CLIENT_QUOTA_STATUS",
    "QUOTA_STATUSES",
    "classify_quota",
    "find_reset_hint",
    "quota_error_body",
    "quota_message",
]
