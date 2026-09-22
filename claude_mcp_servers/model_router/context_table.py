# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The version-keyed chat-model context table.

Claude Code sizes its context indicator and its ``/compact`` thresholds from
the window it believes the selected model has. For an id it does not recognise
it assumes a conservative default, so a 1M-context vendor model reads as far
fuller than it is and compaction fires early. The client's own convention for
the 1M variant is an ``[1m]`` suffix on the id, and it accepts that suffix on
custom ids — so advertising ``<id>[1m]`` in ``/v1/models`` gives the client the
right assumption.

This table is the AUTHORITY on which ids deserve it, and no longer the sole
input: :func:`model_router.catalog.resolve_window` consults it first, falls
back to the window the upstream states for itself, and finally to the previous
version in the same family. A cited row here still wins over both — which is
what keeps the standing rule about the shipped vendor ("never guess that
vendor's windows") true — but an id nobody has tabulated is no longer
automatically advertised at the client's default.

EXACT keys only, and why that is not pedantry
---------------------------------------------
Lookups are exact-full-model-id. No prefix match, no family wildcard, no
"nearest version". Within one shipped vendor, one minor version has a 1M
window while the previous one has 200K; a ``<family>*`` rule would overstate
the smaller by 5x, and the user would only find out when a long session
silently truncated.

**This is NOT ``MODEL_TOKEN_LIMITS``.** ``weaviate_mcp.chunking.MODEL_TOKEN_LIMITS``
answers a question with the same words and an opposite lookup rule: it covers
EMBEDDING models, sets Ollama's ``num_ctx`` for the chunker, and matches
PARTIALLY on purpose (an Ollama tag varies by quantisation while the
architectural limit does not) — and it is a wire-format input to stored
embeddings, guarded by the chunker-revision sentinel. This table covers CHAT
models, drives what the gateway ADVERTISES, and changing it re-embeds nothing.
The two must not be merged and this one must not reuse that lookup.

Citation is enforced, not requested
-----------------------------------
A row whose ``source`` is empty is IGNORED for the ``[1m]`` decision and named
in a WARNING. Guessing a window is exactly what a version-keyed table exists to
prevent, so an uncited row must not be able to change what the client assumes.
The shipped seed's rows all carry the official vendor page they were read from,
pinned by ``tests/test_model_router_context_table.py``.

File contract. The launcher-side exporter that will WRITE this file is a
separate work package and is not in the tree yet; what ships today is the
READER plus the shipped seed, which is a complete and tested path on its own —
a machine with no export serves the seed, and that is the normal state, not a
degraded one. The shape below is therefore this module's requirement on any
future writer, not a description of an existing counterpart::

    <vct_root>/model-gateway/chat_model_context.json
    {"schema_version": 1,
     "generated_at": "<ISO-8601 UTC>",
     "source": "launcher.db",
     "tombstones": ["<full-model-id>", ...],
     "models": {"<full-model-id>": {"vendor": "<vendor_id>",
                                    "context_window": <int>,
                                    "max_output": <int>,
                                    "window_1m": <bool>,
                                    "source": "<official doc URL>",
                                    "source_note": "<optional caveat>"}}}

Absent file -> the shipped seed. Malformed file -> the shipped seed PLUS a
warning naming the path; never a crash, and never an empty table served as if
it were the truth. The file is re-read when its ``(mtime_ns, size)`` changes,
which is checked once per ``/v1/models`` call.

**A present export does not hide the seed.** Precedence is per ROW: the export
wins for every id it names, and an id it does not name falls through to the
shipped seed (:meth:`ContextTable.lookup`). v0.2.93 shipped whole-file
precedence and it cost the feature it was written for — the launcher's
exporter writes vendor rows only, so on every upgraded install the export
existed, carried no Claude row, and every first-party 1M model was therefore
advertised at the client's default window.

**...which is why DELETION needs its own word.** With per-row fallback, an id
the user removes in the GUI is simply an id the export no longer names — and
"no longer names" is exactly the state that falls through to the seed, so the
row would come back. ``tombstones`` is the export writer's way of saying
DELETED rather than ABSENT: a listed id resolves to nothing, seed included.
An absent ``tombstones`` key is an empty list, so an export written by an
older launcher keeps working unchanged.

**An id with no row anywhere falls through to the catalog's remaining
sources** — the window the upstream publishes for itself, then the previous
version in the same family — and gets no ``[1m]`` advert when neither has
anything to say. That last case is the honest outcome: Claude Code then
applies its own conservative assumption (200K) for an id it does not
recognise. The gateway still invents nothing; it just no longer treats an
absent ROW as an absent WINDOW when the model's own publisher stated one.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional

from .model_family import is_older_sibling, parse_model_id
from .routing import split_namespace
from .vendors import VENDORS

logger = logging.getLogger(__name__)

#: Schema versions this reader understands.
SUPPORTED_SCHEMA_VERSIONS = (1,)

#: Shipped fallback, beside this module (so it travels inside the wheel).
SEED_PATH = Path(__file__).resolve().parent / "chat_model_context.seed.json"

#: ``source`` values for :attr:`ContextTable.source`.
SOURCE_EXPORT = "export"
SOURCE_SEED_NO_EXPORT = "seed(no-export)"
SOURCE_SEED_MALFORMED = "seed(export-malformed)"
SOURCE_SEED_UNSUPPORTED = "seed(export-schema-unsupported)"
SOURCE_NONE = "none"

#: The window Claude Code assumes when an id carries the ``[1m]`` suffix, and
#: therefore the threshold a resolved window must reach to earn one. Defined
#: HERE rather than in :mod:`model_router.catalog` because it is a property of
#: model windows, which is what this module is about; the catalog imports it,
#: so the number exists once in the package.
ONE_M_WINDOW = 1_000_000

#: What to assume for a model no row names and whose family nothing in the
#: table knows. OWNER RULING 2026-09-17, verbatim: "if model from an unknown
#: family I'd assume 256k context until we manually research the true size and
#: add it to the table we have."
#:
#: It is an ASSUMPTION, never a published figure: the gateway's catalog still
#: refuses to invent a window for an id nobody has tabulated
#: (:func:`model_router.catalog.resolve_window` answers ``unknown``), because
#: that number would be shown to the user as if it were read from a vendor
#: page. This one is consumed by a DECISION that has to be made either way —
#: does a settings value get the ``[1m]`` hint — and 256K answers it with "no",
#: which is the conservative direction.
UNKNOWN_FAMILY_WINDOW = 256_000

#: Which step of :meth:`ContextTable.assume_window` produced the answer.
ASSUMED_FROM_TABLE = "table"
ASSUMED_FROM_FAMILY = "family"
ASSUMED_FLOOR = "floor"


@dataclass(frozen=True)
class ModelContext:
    """One row, as the gateway reads it.

    ``context_window`` and ``max_output`` are ACTED ON, not merely carried:
    :func:`model_router.catalog.resolve_window` takes this row as the highest
    authority below a tombstone, and the window it yields decides both the
    row's ``description`` and whether it earns an ``[1m]`` advert. (Until
    v0.2.95 only ``window_1m`` was read, and the note here said so; the two
    fields could therefore contradict each other unnoticed, which is why the
    catalog now warns when they do.)

    ``window_1m`` remains the field
    :func:`vco_lib.vscode_settings.decorate_1m` reads, so it is still a
    published part of the schema and still worth keeping honest — but the
    gateway derives its own advert from ``context_window``, because that is
    the number the citation is for."""

    model_id: str
    vendor: str
    context_window: int
    max_output: int
    window_1m: bool
    source: str
    source_note: str = ""


@dataclass(frozen=True)
class AssumedWindow:
    """What to ASSUME about one model id's context window, and from where.

    Distinct from :class:`model_router.catalog.WindowResolution`, and the
    difference is the point of both: that one may answer "unknown", because it
    feeds what the gateway PUBLISHES and publishing a number nobody verified
    would be an invention. This one always answers, because it feeds a binary
    decision — does this settings value carry the client's ``[1m]`` hint — and
    a caller who must decide is better served by a stated assumption than by a
    ``None`` it would have to turn into one anyway, differently each time.

    Attributes:
        window: tokens. Never ``None``; see above.
        source: :data:`ASSUMED_FROM_TABLE`, :data:`ASSUMED_FROM_FAMILY` or
            :data:`ASSUMED_FLOOR`.
        inherited_from: the sibling whose window was inherited, when
            ``source`` is :data:`ASSUMED_FROM_FAMILY`; ``None`` otherwise.
    """

    window: int
    source: str
    inherited_from: Optional[str] = None


@dataclass(frozen=True)
class ContextTable:
    """An immutable snapshot of the table plus where it came from."""

    rows: Mapping[str, ModelContext]
    source: str
    #: Path actually read, or ``None`` when nothing could be read at all.
    path: Optional[Path]
    #: Ids dropped for lacking a citation. Surfaced so the GUI can show them.
    uncited: tuple[str, ...] = ()
    #: Seed rows consulted for an id the ACTIVE table has no row for. Set
    #: only when the active table came from an export. See
    #: :meth:`lookup` for why per-row rather than whole-file precedence.
    fallback_rows: Mapping[str, ModelContext] = field(default_factory=dict)
    #: Ids the export declares DELETED. Distinct from absent: absent falls
    #: through to the seed, deleted resolves to nothing at all.
    tombstones: frozenset[str] = frozenset()

    def lookup(self, model_id: str) -> Optional[ModelContext]:
        """EXACT match only. ``glm-5.1-flash-x`` never resolves to ``glm-5.1``.

        Precedence is PER ROW, not per file: an export row wins for the id it
        names, and an id the export does not name falls through to the shipped
        seed. The alternative — whole-file precedence — is what shipped in
        v0.2.93 and it silently lost data: the launcher's exporter writes the
        ten vendor rows it knows about, so on every upgraded install the
        export had no Claude rows at all and every first-party 1M model was
        advertised as if it were 200K. An absent row means "this table has
        nothing to say about that id", never "that id has no 1M window".
        """
        if model_id in self.tombstones:
            # Checked BEFORE the fallback, which is the whole point: the user
            # deleted this row in the GUI, and a per-row fallback that did not
            # know the difference between "deleted" and "absent" would hand it
            # straight back from the seed.
            return None
        row = self.rows.get(model_id)
        if row is None:
            return self.fallback_rows.get(model_id)
        return row

    def assume_window(self, model_id: str) -> AssumedWindow:
        """The window to ASSUME for ``model_id``. Always answers.

        Three steps, in the order the OWNER RULED on 2026-09-17 (verbatim:
        "new model versions inherit the highest context in the family (i.e.
        any new Sonnet model has 1m even if old sonnet used to have 256k), if
        model from an unknown family I'd assume 256k context until we manually
        research the true size and add it to the table we have"):

        1. **this id's own row**, exact match, tombstone-aware — the table
           stays the authority for every model it names, so adding a row is
           still how anyone corrects this answer, and a named model NEVER
           inherits anything (the ruling's "until we manually research it"
           only ends when a row exists). A row whose window is not a positive
           number says nothing usable and falls through rather than handing a
           caller a zero to act on;
        2. **the HIGHEST window among strictly-older members of the same
           family** (:func:`model_router.model_family.is_older_sibling`) — the
           auto-update half of the ruling. A ``claude-sonnet-6`` that ships
           tomorrow is assumed 1M because ``claude-sonnet-5`` is, without
           anyone editing a file; ``claude-haiku-5`` looks only at haiku rows,
           so one family's jump to 1M never leaks into another's;
        3. **:data:`UNKNOWN_FAMILY_WINDOW`** — nothing here knows this family.

        A TOMBSTONED id short-circuits to step 3 before any of this: the
        export declared it deleted, and a deletion a sibling could undo is
        not a deletion.

        Siblings are restricted to the queried id's VENDOR whenever the id
        carries a vendor namespace, because two vendors may publish the same
        family stem and an id alone cannot tell them apart. For a bare id (no
        namespace) that restriction is unavailable, and the family stem is all
        there is; the failure case is therefore narrow and named: a bare query
        whose family stem is also published by a SECOND vendor in the same
        table would pool both vendors' rows and take the larger window.

        **The shipped table DOES have such a pair, deliberately** (owner
        ruling, 2026-09-22): ``glm-5.3`` and ``glm-5.2`` are listed by both
        the z.ai row and the QwenCloud row, and the cited z.ai window
        answers for both. The ruling is that a context window is a property
        of the MODEL, not of the endpoint serving it — glm-5.3 is glm-5.3
        wherever it is served — so the rows are NOT narrowed to match the
        weaker citation, and ``lookup`` stays vendor-blind on purpose. Pinned
        by ``tests/test_model_router_context_table.py::
        test_the_shared_ids_answer_from_the_one_row_that_exists``.

        The cost of that ruling, stated so nobody has to rediscover it: if an
        endpoint serves a shared model at a SMALLER window than the citation,
        the picker over-advertises and the client budgets more context than
        the upstream will accept. Keying ``lookup`` on ``(vendor, id)`` — the
        row's ``vendor`` field exists for exactly that — is the refinement to
        reach for if that ever bites.
        """
        parts = parse_model_id(model_id)
        if parts.bare_id in self.tombstones:
            # "Deleted, seed included" has to mean deleted, FAMILY included:
            # inheriting a sibling's window would hand back a 1M claim for the
            # exact id the user removed in the GUI, which is the same
            # deleted-is-not-absent argument :meth:`lookup` makes one level
            # up. The floor is what is left to assume, and it is the
            # conservative answer.
            return AssumedWindow(UNKNOWN_FAMILY_WINDOW, ASSUMED_FLOOR)
        row = self.lookup(parts.bare_id)
        if row is not None and row.context_window > 0:
            return AssumedWindow(row.context_window, ASSUMED_FROM_TABLE)

        vendor, _remainder = split_namespace(model_id, VENDORS)
        vendor_id = vendor.vendor_id if vendor is not None else None
        best: Optional[ModelContext] = None
        for candidate in self._known_rows():
            if vendor_id is not None and candidate.vendor != vendor_id:
                continue
            if candidate.context_window <= 0:
                continue
            if not is_older_sibling(parse_model_id(candidate.model_id), parts):
                continue
            if (
                best is None
                or candidate.context_window > best.context_window
                # Deterministic tie-break: two siblings with the same window
                # must not make the answer depend on dict order.
                or (
                    candidate.context_window == best.context_window
                    and candidate.model_id < best.model_id
                )
            ):
                best = candidate
        if best is not None:
            return AssumedWindow(
                best.context_window, ASSUMED_FROM_FAMILY, best.model_id,
            )
        return AssumedWindow(UNKNOWN_FAMILY_WINDOW, ASSUMED_FLOOR)

    def _known_rows(self) -> tuple[ModelContext, ...]:
        """Every row a lookup could answer with: export first, seed behind it.

        Tombstoned ids are excluded here as well as in :meth:`lookup` — a row
        the user deleted must not come back as somebody else's inherited
        window, which is the same "deleted is not absent" argument one level
        up.
        """
        merged: dict[str, ModelContext] = {}
        for source in (self.fallback_rows, self.rows):
            for model_id, row in source.items():
                if model_id in self.tombstones:
                    continue
                merged[model_id] = row
        return tuple(merged.values())

    def advertise_1m(self, model_id: str) -> bool:
        """Should this id carry Claude Code's ``[1m]`` hint?

        One consumer: :func:`vco_lib.vscode_settings.decorate_1m`, which
        appends the hint to a settings value naming a 1M model.

        A row in the table answers with its own ``window_1m`` FLAG — that
        field is the published schema and the table is the authority for
        every model it names. An id NO row names is answered from
        :meth:`assume_window`, which NARROWS — it does not close — the
        asymmetry this docstring used to describe: before v0.2.95 a 1M model
        with no row was advertised by the gateway (whose catalog resolves a
        window) and NOT decorated in the settings file, so a brand-new 1M
        model sat in the picker with a 200K budget until somebody edited a
        table. It now inherits its family's highest TABULATED window, and an
        unknown family assumes :data:`UNKNOWN_FAMILY_WINDOW` — below 1M, so
        the answer there is "no", which is the conservative direction.

        What remains, deliberately: the catalog's own family floor
        (``model_router.catalog._family_floor``) also inherits from an
        UPSTREAM-STATED window, and this table cannot see one — nothing here
        reads the network, by design (below). So a vendor family with no
        cited row, whose upstream states 1M, is still published ``[1m]`` in
        the picker and still assumes 256K here. That residue is the OWNER'S
        RULED behaviour for an unknown family, not a gap to close: a cited
        row is how a vendor family gets verified, and the pair it narrows to
        is small — Anthropic's ``/v1/models`` states no window at all, the
        panel Default is first-party only, and a slot value is the one path
        that reaches this function.

        No network, in either branch: the resolution reads this table and
        nothing else, so a settings write can never block on (or be wrong
        because of) a gateway that is not running.
        """
        row = self.lookup(parse_model_id(model_id).bare_id)
        if row is not None:
            return bool(row.window_1m)
        return self.assume_window(model_id).window >= ONE_M_WINDOW


def _parse(
    payload: object, origin: Path,
) -> tuple[dict[str, ModelContext], tuple[str, ...], tuple[str, ...]]:
    """Parse a table document. Raises ``ValueError`` on a shape it cannot use.

    Returns ``(rows, uncited, tombstones)``.
    """
    if not isinstance(payload, dict):
        raise ValueError("top level is not an object")
    version = payload.get("schema_version")
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise _UnsupportedSchema(
            f"schema_version {version!r} is not one of "
            f"{SUPPORTED_SCHEMA_VERSIONS!r}",
        )
    models = payload.get("models")
    if not isinstance(models, dict):
        raise ValueError("'models' is missing or not an object")

    raw_tombstones = payload.get("tombstones")
    tombstones: tuple[str, ...] = ()
    if isinstance(raw_tombstones, list):
        tombstones = tuple(
            entry for entry in raw_tombstones if isinstance(entry, str) and entry
        )
    elif raw_tombstones is not None:
        logger.warning(
            "model-gateway: 'tombstones' in %s is not a list; ignored",
            origin,
        )

    rows: dict[str, ModelContext] = {}
    uncited: list[str] = []
    for model_id, raw in models.items():
        if not isinstance(model_id, str) or model_id.startswith("_"):
            continue
        if not isinstance(raw, dict):
            logger.warning(
                "model-gateway: chat-model context row %r in %s is not an "
                "object; ignored",
                model_id, origin,
            )
            continue
        source = str(raw.get("source") or "").strip()
        if not source:
            uncited.append(model_id)
            logger.warning(
                "model-gateway: chat-model context row %r in %s has no "
                "'source' citation; ignored (an uncited window is a guess, and "
                "the client would act on it)",
                model_id, origin,
            )
            continue
        try:
            context_window = int(raw.get("context_window") or 0)
            max_output = int(raw.get("max_output") or 0)
        except (TypeError, ValueError):
            logger.warning(
                "model-gateway: chat-model context row %r in %s has "
                "non-numeric window/output; ignored",
                model_id, origin,
            )
            continue
        rows[model_id] = ModelContext(
            model_id=model_id,
            vendor=str(raw.get("vendor") or ""),
            context_window=context_window,
            max_output=max_output,
            window_1m=bool(raw.get("window_1m")),
            source=source,
            source_note=str(raw.get("source_note") or ""),
        )
    return rows, tuple(uncited), tombstones


class _UnsupportedSchema(ValueError):
    """The document parsed but declares a schema this reader does not know."""


def load_seed() -> ContextTable:
    """The shipped table. A broken seed is a broken BUILD, so it is loud."""
    try:
        payload = json.loads(SEED_PATH.read_text(encoding="utf-8"))
        rows, uncited, _tombstones = _parse(payload, SEED_PATH)
    except (OSError, ValueError) as exc:
        # The seed ships inside the wheel and is pinned by a packaging test.
        # If it is unreadable the install is damaged; say so and serve an
        # empty table rather than pretending every model has a default window.
        logger.error(
            "model-gateway: shipped context seed %s is unreadable (%s). No "
            "model will be advertised with a 1M window. This is a damaged "
            "install: re-run `python install.py`.",
            SEED_PATH, exc,
        )
        return ContextTable(rows={}, source=SOURCE_NONE, path=None)
    return ContextTable(
        rows=rows, source=SOURCE_SEED_NO_EXPORT, path=SEED_PATH, uncited=uncited,
    )


class ContextTableLoader:
    """Loads the export when present, the seed otherwise; re-reads on change."""

    def __init__(self, export_path: Path) -> None:
        self._export_path = export_path
        self._stamp: tuple[int, int] | None = None
        self._table: ContextTable | None = None
        self._seed: ContextTable | None = None

    @property
    def export_path(self) -> Path:
        return self._export_path

    def _seed_table(self, source: str) -> ContextTable:
        if self._seed is None:
            self._seed = load_seed()
        return ContextTable(
            rows=self._seed.rows,
            source=source if self._seed.rows else SOURCE_NONE,
            path=self._seed.path,
            uncited=self._seed.uncited,
        )

    def current(self) -> ContextTable:
        """The table as of now. One ``stat`` when nothing changed."""
        try:
            st = os.stat(self._export_path)
        except OSError:
            # No export (yet, or any more). Rebuild the seed view when the
            # previous answer came from an export that has since gone away.
            table = self._table
            if table is None or self._stamp is not None:
                self._stamp = None
                table = self._seed_table(SOURCE_SEED_NO_EXPORT)
                self._table = table
            return table

        stamp = (st.st_mtime_ns, st.st_size)
        if stamp == self._stamp and self._table is not None:
            return self._table
        self._stamp = stamp

        try:
            payload = json.loads(self._export_path.read_text(encoding="utf-8"))
            rows, uncited, tombstones = _parse(payload, self._export_path)
        except _UnsupportedSchema as exc:
            logger.warning(
                "model-gateway: chat-model context export %s declares an "
                "unsupported schema (%s); using the shipped seed instead. "
                "Update the orchestrator, or delete the file to silence this.",
                self._export_path, exc,
            )
            self._table = self._seed_table(SOURCE_SEED_UNSUPPORTED)
            return self._table
        except (OSError, ValueError) as exc:
            logger.warning(
                "model-gateway: chat-model context export %s is unreadable "
                "(%s); using the shipped seed instead.",
                self._export_path, exc,
            )
            self._table = self._seed_table(SOURCE_SEED_MALFORMED)
            return self._table

        self._table = ContextTable(
            rows=rows,
            source=SOURCE_EXPORT,
            path=self._export_path,
            uncited=uncited,
            fallback_rows=self._seed_rows(),
            tombstones=frozenset(tombstones),
        )
        return self._table

    def _seed_rows(self) -> Mapping[str, ModelContext]:
        """The shipped rows, as the per-row fallback under an export.

        One home: the seed is loaded once here and handed to every table the
        loader builds, so nothing else in the package needs to know that a
        fallback exists — ``ContextTable.lookup`` is the only reader.
        """
        if self._seed is None:
            self._seed = load_seed()
        return self._seed.rows


__all__ = [
    "SEED_PATH",
    "SOURCE_EXPORT",
    "SOURCE_NONE",
    "SOURCE_SEED_MALFORMED",
    "SOURCE_SEED_NO_EXPORT",
    "SOURCE_SEED_UNSUPPORTED",
    "SUPPORTED_SCHEMA_VERSIONS",
    "ContextTable",
    "ContextTableLoader",
    "ModelContext",
    "load_seed",
]
