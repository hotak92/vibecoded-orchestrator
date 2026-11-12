# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Split-on-refusal: the ONE home for embedding a text a model refuses.

Owner rulings 2026-11-11 ("split on refusal"; then "extra rows in both
places, so we know which chunk actually matched"): when an embedding model
refuses an input because it exceeds its context window, the input is SPLIT in
two at the best boundary nearest its middle and both halves are embedded,
recursively, until every piece is accepted. Nothing is cut off, and a stored
row's vector is never an average of piece vectors.

Rows (every writer of stored rows): the KG and document writers (kg-sync's
node and document paths, the MCP ``store_knowledge_node``, the shipped
sidecar builder, ``process_documents``) and the code-graph analyzer turn each
refused chunk into MORE ROWS of the same node or entity, in text order:
:func:`embed_rows` / :func:`embed_rows_async`. Inside a
:func:`row_split_scope`, EVERY document leg of the embedding service (the
active model, each enabled secondary slot, the carry model of a model fill,
the code legs) raises :class:`ChunkOverflowError` on a refusal instead of
handling it, and the row driver splits that chunk. The row layout is
therefore the FINEST split any model of the row's vector set needs: if one
model refuses chunk C, C is split for all of them, and every resulting row
carries every model's vector. Deterministic for a given model set
(:func:`split_point` is pure, a model's refusal of a given text is
deterministic). Adding a model can only refine the layout (a piece every
model accepted before is split further only if the new model refuses it);
removing one keeps the finer rows, which stay exact for the remaining models.

One-vector consumers OUTSIDE a row writer (``embedding_service.
_embed_whole_on_overflow``): a caller that needs exactly one vector for a
text that is not a stored row (the RL n_emb recovery and slot regen of an
existing row for a model outside its write set, the legacy migrate script,
the MCP ``backfill_embeddings`` tool) still gets :func:`embed_split_pieces`
+ :func:`combine_vectors` — every character reaches the vector. Search
queries never come here (a query keeps its leading window by design).

Floor and depth: a piece of at most :data:`SPLIT_FLOOR_CHARS` characters that
is STILL refused is not a window problem (no supported model has a window
that small); it raises :class:`UnsplittableTextError` — the caller reports
it (the node fails, its old rows stay in place), it is never silently
dropped. :data:`MAX_SPLIT_DEPTH` bounds the recursion independently.

Layout records: :func:`row_layout` (``{"<1-based plan chunk>": [piece
lengths]}``) is what the shipped sidecar stores so an install rebuilds the
identical rows without asking a model (:func:`apply_row_layout`);
:func:`stored_rows_cover_plan` recognises stored rows that are the plan's
chunks or consecutive pieces of them (kg-sync's plan check, the code-graph
analyzer's skip of an unchanged split entity); :func:`join_row_texts` glues
consecutive rows back into text (a continuation piece with nothing in
between).
"""

from __future__ import annotations

import contextlib
import contextvars
import math
import re
from dataclasses import dataclass
from typing import (
    Any,
    Awaitable,
    Callable,
    Generator,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    TypeVar,
)

T = TypeVar("T")

#: Characters at or below which a refused piece is reported, not split again.
#: ONE home: ``embedding_service._EMBED_RETRY_FLOOR_CHARS`` (the query shrink
#: ladder's floor) is this value.
SPLIT_FLOOR_CHARS: int = 512

#: Hard bound on the recursion depth. Each split leaves both halves at most
#: three quarters of the parent (see :func:`split_point`), so 32 levels cover
#: inputs far beyond any chunk; the floor normally stops the recursion first.
MAX_SPLIT_DEPTH: int = 32


class ChunkOverflowError(RuntimeError):
    """A model of the row's vector set refused a whole chunk inside a
    :func:`row_split_scope` (the active model, an enabled secondary, the carry
    of a model fill, a code leg).

    Raised by the embedding service instead of handling the refusal itself,
    so the row driver can split the chunk into more rows. Carries the
    refusing model and the refused length for the fidelity summary.
    """

    def __init__(self, model_id: str, chars: int, cause: str = "") -> None:
        self.model_id = model_id
        self.chars = int(chars)
        self.cause = cause
        super().__init__(
            f"{model_id} refused a {chars}-char chunk (context window "
            f"exceeded): {cause}"
        )


class UnsplittableTextError(RuntimeError):
    """A piece at the floor (or at the depth bound) was still refused.

    The caller must surface it — the text it belongs to gets no vector, and
    a row writer fails the node with its old rows left in place.
    """

    def __init__(self, model_id: str, chars: int, cause: str = "") -> None:
        self.model_id = model_id
        self.chars = int(chars)
        self.cause = cause
        super().__init__(
            f"{model_id or 'embedding model'} still refused a {chars}-char "
            f"piece after splitting (at the {SPLIT_FLOOR_CHARS}-char floor or "
            f"the depth limit) — this text gets no vector: {cause}"
        )


# ---------------------------------------------------------------------------
# Boundaries
# ---------------------------------------------------------------------------

#: Boundary classes in preference order. Each pattern's match END is a split
#: position, so the separator stays with the LEFT piece and
#: ``left + right == text`` always holds.
_BOUNDARY_PATTERNS: Tuple["re.Pattern[str]", ...] = (
    re.compile(r"\n[ \t]*\n\s*"),                      # paragraph
    re.compile(r"[.!?]+[\"')\]]*\s+|[。！？]+\s*"),      # sentence
    re.compile(r"\n"),                                  # line
    re.compile(r"\s+"),                                 # whitespace
)


def _is_high_surrogate(ch: str) -> bool:
    return "\ud800" <= ch <= "\udbff"


def _is_low_surrogate(ch: str) -> bool:
    return "\udc00" <= ch <= "\udfff"


def _safe_cut(text: str, pos: int) -> bool:
    """A cut at ``pos`` keeps every code point (and surrogate pair) whole.

    A Python ``str`` is a sequence of code points, so an index never falls
    inside a UTF-8 multi-byte sequence; the one way to break a character is
    a lone surrogate pair in text that was decoded with ``surrogatepass`` (or
    built from UTF-16 data), which this rejects.
    """
    if pos <= 0 or pos >= len(text):
        return False
    return not (_is_high_surrogate(text[pos - 1]) and _is_low_surrogate(text[pos]))


def split_point(text: str) -> int:
    """The index to split ``text`` at: the best boundary nearest the middle.

    Preference: paragraph break > sentence end > line break > whitespace >
    a hard cut at the middle. Only boundaries in the middle half
    (``[n/4, 3n/4]``) qualify, so each piece is at most three quarters of the
    input and the recursion always makes progress. Ties go to the EARLIER
    position. Pure and deterministic. Raises ``ValueError`` for a text of
    fewer than two characters (nothing to split).
    """
    n = len(text)
    if n < 2:
        raise ValueError("a text of fewer than 2 characters cannot be split")
    mid = n // 2
    lo = max(1, n // 4)
    hi = min(n - 1, n - n // 4)
    for pattern in _BOUNDARY_PATTERNS:
        best: Optional[int] = None
        for match in pattern.finditer(text, lo - 1 if lo > 1 else 0):
            pos = match.end()
            if pos < lo:
                continue
            if pos > hi:
                break
            if not _safe_cut(text, pos):
                continue
            if best is None or abs(pos - mid) < abs(best - mid):
                best = pos
        if best is not None:
            return best
    pos = mid
    if not _safe_cut(text, pos):
        pos += 1  # step past the low surrogate; n >= 2 keeps pos < n here
    return pos


def split_in_two(text: str) -> Tuple[str, str]:
    """``(left, right)`` with ``left + right == text``, cut at :func:`split_point`."""
    pos = split_point(text)
    return text[:pos], text[pos:]


def pieces_within(text: str, max_chars: int) -> List[str]:
    """Split ``text`` (recursively, at :func:`split_point`) until every piece
    is at most ``max_chars`` long. ``max_chars <= 0`` returns ``[text]``.

    Used where an ESTIMATE already says the text is too long for a model
    (a secondary slot's char budget, a batch's pre-bound), so the round trip
    of a certain refusal is skipped. Deterministic; the pieces concatenate to
    ``text``.
    """
    if max_chars <= 0 or len(text) <= max_chars or len(text) < 2:
        return [text]
    left, right = split_in_two(text)
    return pieces_within(left, max_chars) + pieces_within(right, max_chars)


# ---------------------------------------------------------------------------
# One vector for a text (split + combine)
# ---------------------------------------------------------------------------


def embed_split_pieces(
    embed_fn: Callable[[str], T],
    text: str,
    *,
    is_overflow: Callable[[BaseException], bool],
    model_id: str = "",
    pre_split_chars: int = 0,
    floor_chars: int = SPLIT_FLOOR_CHARS,
    max_depth: int = MAX_SPLIT_DEPTH,
) -> List[Tuple[str, T]]:
    """Embed ``text``, splitting every refused piece in two until accepted.

    Returns ``[(piece, embed_fn(piece)), ...]`` in text order; the pieces
    concatenate to ``text``. ``pre_split_chars`` (> 0) first splits the text
    to pieces of at most that many characters without asking the model.

    Only an exception ``is_overflow`` recognises is answered with a split;
    every other exception propagates untouched (splitting is the remedy for
    "too long", not for an auth error or a down backend). A refused piece of
    at most ``floor_chars`` characters, or one at ``max_depth``, raises
    :class:`UnsplittableTextError`.
    """
    out: List[Tuple[str, T]] = []

    def _go(piece: str, depth: int) -> None:
        try:
            out.append((piece, embed_fn(piece)))
            return
        except Exception as exc:  # noqa: BLE001 — re-raised unless an overflow
            if not is_overflow(exc):
                raise
            if len(piece) <= floor_chars or depth >= max_depth or len(piece) < 2:
                raise UnsplittableTextError(model_id, len(piece), str(exc)) from exc
        left, right = split_in_two(piece)
        _go(left, depth + 1)
        _go(right, depth + 1)

    for start in pieces_within(text, pre_split_chars):
        _go(start, 0)
    return out


def combine_vectors(pieces: Sequence[Tuple[str, Sequence[float]]]) -> List[float]:
    """One vector for a split text: the piece vectors averaged by piece length.

    Weighted by character count so a short tail does not count as much as
    the bulk of the text, then L2-normalised (the stored vectors are compared
    by cosine, which a uniform scale does not change; normalising keeps the
    combined vector on the same unit sphere the model's own vectors lie on).
    A single piece is returned unchanged, so a text that fit produces exactly
    the vector it produced before this module existed.
    """
    if not pieces:
        raise ValueError("combine_vectors needs at least one piece")
    if len(pieces) == 1:
        return list(pieces[0][1])
    dim = len(pieces[0][1])
    acc = [0.0] * dim
    total_weight = 0
    for piece_text, vec in pieces:
        if len(vec) != dim:
            raise ValueError(
                f"piece vectors disagree on dimension ({len(vec)} != {dim})"
            )
        weight = max(1, len(piece_text))
        total_weight += weight
        for i, value in enumerate(vec):
            acc[i] += weight * float(value)
    mean = [value / total_weight for value in acc]
    norm = math.sqrt(sum(value * value for value in mean))
    if norm == 0.0:
        return mean
    return [value / norm for value in mean]


# ---------------------------------------------------------------------------
# Rows (split into more rows of the same node)
# ---------------------------------------------------------------------------

_ROW_SPLIT: "contextvars.ContextVar[bool]" = contextvars.ContextVar(
    "vco_embed_row_split", default=False
)


@contextlib.contextmanager
def row_split_scope() -> Iterator[None]:
    """While active, every document embed leg raises :class:`ChunkOverflowError`
    on a refusal: the active model, each enabled secondary, the carry model of
    a model fill and the code legs (``embedding_service.
    _embed_whole_or_refuse``).

    A ``ContextVar``, so it follows ``asyncio.to_thread`` (which copies the
    caller's context) into the worker thread the MCP embeds in, and never
    leaks into another task or thread.
    """
    token = _ROW_SPLIT.set(True)
    try:
        yield
    finally:
        _ROW_SPLIT.reset(token)


def row_split_active() -> bool:
    """True inside :func:`row_split_scope`."""
    return _ROW_SPLIT.get()


@dataclass(frozen=True)
class RowPiece:
    """One stored row: a piece of plan chunk ``source_index``.

    ``piece_index`` / ``piece_count`` locate it inside that chunk (0 / 1 for
    a chunk the model accepted whole). ``payload`` is what the writer's
    embed function returned for ``text``.
    """

    text: str
    source_index: int
    piece_index: int
    piece_count: int
    payload: Any


def _row_driver(
    texts: Sequence[str],
    floor_chars: int,
    max_depth: int,
) -> Generator[str, Tuple[bool, Any], List[RowPiece]]:
    """The recursion, written once for the sync and async runners.

    Yields a text to embed; the runner sends back ``(True, payload)`` or
    ``(False, exception)``. Returns the rows in order.
    """
    rows: List[RowPiece] = []
    for source_index, source in enumerate(texts):
        accepted: List[Tuple[str, Any]] = []
        refusing_model = ""
        stack: List[Tuple[str, int]] = [(source, 0)]
        while stack:
            piece, depth = stack.pop()
            ok, value = yield piece
            if ok:
                accepted.append((piece, value))
                continue
            exc = value
            if not isinstance(exc, ChunkOverflowError):
                raise exc
            refusing_model = exc.model_id or refusing_model
            if len(piece) <= floor_chars or depth >= max_depth or len(piece) < 2:
                _note_unsplittable(exc.model_id, len(piece), exc.cause)
                raise UnsplittableTextError(
                    exc.model_id, len(piece), exc.cause
                ) from exc
            left, right = split_in_two(piece)
            # LIFO: push right first so left is embedded (and stored) first.
            stack.append((right, depth + 1))
            stack.append((left, depth + 1))
        if len(accepted) > 1:
            _note_row_split(refusing_model, len(source), len(accepted))
        count = len(accepted)
        for piece_index, (piece, payload) in enumerate(accepted):
            rows.append(RowPiece(piece, source_index, piece_index, count, payload))
    return rows


def _note_row_split(model_id: str, chars: int, pieces: int) -> None:
    """Record a row split in the run's fidelity summary. Never raises."""
    try:
        from vco_lib import embedding_fidelity

        embedding_fidelity.note_split(model_id or "active", chars, pieces)
    except Exception:  # noqa: BLE001 — telemetry never breaks the embed
        pass


def _note_unsplittable(model_id: str, chars: int, cause: str) -> None:
    try:
        from vco_lib import embedding_fidelity

        embedding_fidelity.note_floor_refusal(model_id, chars, cause)
    except Exception:  # noqa: BLE001 — telemetry never breaks the embed
        pass



def embed_rows(
    texts: Sequence[str],
    embed_fn: Callable[[str], Any],
    *,
    floor_chars: int = SPLIT_FLOOR_CHARS,
    max_depth: int = MAX_SPLIT_DEPTH,
) -> List[RowPiece]:
    """Embed every plan chunk in ``texts``; a refused chunk becomes more rows.

    ``embed_fn`` runs inside :func:`row_split_scope`, so every model the
    service embeds a row with raises :class:`ChunkOverflowError` on a
    refusal; that chunk is split at :func:`split_point` and both halves are
    embedded with every model again, recursively — the finest split any
    model of the row's vector set needs.
    Every row is embedded BEFORE this returns, so a writer that calls it
    before touching the stored rows never deletes a node it cannot replace.
    """
    with row_split_scope():
        driver = _row_driver(texts, floor_chars, max_depth)
        try:
            piece = next(driver)
            while True:
                try:
                    result: Tuple[bool, Any] = (True, embed_fn(piece))
                except ChunkOverflowError as exc:
                    result = (False, exc)
                piece = driver.send(result)
        except StopIteration as stop:
            return list(stop.value)


async def embed_rows_async(
    texts: Sequence[str],
    embed_fn: Callable[[str], Awaitable[Any]],
    *,
    floor_chars: int = SPLIT_FLOOR_CHARS,
    max_depth: int = MAX_SPLIT_DEPTH,
) -> List[RowPiece]:
    """The async twin of :func:`embed_rows` (same driver, same rows)."""
    with row_split_scope():
        driver = _row_driver(texts, floor_chars, max_depth)
        try:
            piece = next(driver)
            while True:
                try:
                    result: Tuple[bool, Any] = (True, await embed_fn(piece))
                except ChunkOverflowError as exc:
                    result = (False, exc)
                piece = driver.send(result)
        except StopIteration as stop:
            return list(stop.value)


# ---------------------------------------------------------------------------
# Row layout: which plan chunks were split, and how
# ---------------------------------------------------------------------------


def row_layout(rows: Sequence[RowPiece]) -> "dict[str, list[int]]":
    """``{"<1-based plan chunk>": [piece lengths]}`` for every SPLIT chunk.

    An unsplit chunk is absent, so a layout of ``{}`` means "one row per
    plan chunk". This is what the shipped-embedding sidecar records so the
    ingest can rebuild exactly the rows the builder embedded.
    """
    layout: "dict[str, list[int]]" = {}
    for row in rows:
        if row.piece_count > 1:
            layout.setdefault(str(row.source_index + 1), []).append(len(row.text))
    return layout


def apply_row_layout(
    texts: Sequence[str], layout: Optional[Mapping[str, Any]]
) -> Optional[List[Tuple[int, str]]]:
    """Rebuild the rows ``layout`` describes for ``texts`` (the plan chunks).

    Returns ``[(source_index, row_text), ...]`` or ``None`` when the layout
    does not describe these texts (a key out of range, lengths that do not
    sum to the chunk's length, a non-positive length) — the caller then
    embeds locally rather than ingesting rows that do not match.
    """
    if layout is None:
        layout = {}
    if not isinstance(layout, Mapping):
        return None
    out: List[Tuple[int, str]] = []
    try:
        wanted = {int(k): v for k, v in layout.items()}
    except (TypeError, ValueError):
        return None
    if any(k < 1 or k > len(texts) for k in wanted):
        return None
    for index, text in enumerate(texts):
        lengths = wanted.get(index + 1)
        if lengths is None:
            out.append((index, text))
            continue
        if (
            not isinstance(lengths, list)
            or len(lengths) < 2
            or not all(isinstance(n, int) and not isinstance(n, bool) and n > 0
                       for n in lengths)
            or sum(lengths) != len(text)
        ):
            return None
        pos = 0
        for n in lengths:
            out.append((index, text[pos:pos + n]))
            pos += n
    return out


def stored_rows_cover_plan(
    stored_contents: Sequence[Optional[str]], plan_contents: Sequence[str]
) -> bool:
    """True when the stored rows (in chunk order) are the plan's chunks, each
    either whole or split into consecutive pieces.

    The plan-comparison check uses it: a chunk the model refused is stored as
    several rows whose texts concatenate to that chunk, and those rows are as
    current as an unsplit one.
    """
    position = 0
    for plan_text in plan_contents:
        built = ""
        while len(built) < len(plan_text):
            if position >= len(stored_contents):
                return False
            piece = stored_contents[position]
            if not isinstance(piece, str) or not piece:
                return False
            built += piece
            position += 1
        if built != plan_text:
            return False
    return position == len(stored_contents)


def join_row_texts(
    texts: Sequence[str],
    continuation: Sequence[bool],
    separator: str = "\n",
) -> str:
    """Join consecutive stored row texts back into readable text.

    A CONTINUATION piece (the 2nd..nth row a refused chunk was split into)
    is glued to the row before it with NOTHING in between: split pieces
    concatenate exactly to their chunk, so a separator would corrupt the
    text wherever the split point was a hard cut (base64, a long
    identifier). Rows that start a chunk are joined with ``separator``.
    ``continuation`` aligns with ``texts``; a length mismatch joins
    everything with ``separator`` (the behaviour before split rows existed).
    """
    if not texts:
        return ""
    if len(continuation) != len(texts):
        return separator.join(texts)
    out = [texts[0]]
    for text, glued in zip(texts[1:], continuation[1:]):
        out.append(("" if glued else separator) + text)
    return "".join(out)


__all__ = [
    "ChunkOverflowError",
    "MAX_SPLIT_DEPTH",
    "RowPiece",
    "SPLIT_FLOOR_CHARS",
    "UnsplittableTextError",
    "apply_row_layout",
    "combine_vectors",
    "embed_rows",
    "embed_rows_async",
    "embed_split_pieces",
    "join_row_texts",
    "pieces_within",
    "row_layout",
    "row_split_active",
    "row_split_scope",
    "split_in_two",
    "split_point",
    "stored_rows_cover_plan",
]

