# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The version-keyed chat-model context table.

Claude Code sizes its context indicator and its ``/compact`` thresholds from
the window it believes the selected model has. For an id it does not recognise
it assumes a conservative default, so a 1M-context vendor model reads as far
fuller than it is and compaction fires early. The client's own convention for
the 1M variant is an ``[1m]`` suffix on the id, and it accepts that suffix on
custom ids — so advertising ``<id>[1m]`` in ``/v1/models`` gives the client the
right assumption. This table decides which ids get it.

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
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

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


@dataclass(frozen=True)
class ModelContext:
    """One row. ``window_1m`` is the only field the gateway acts on today;
    the other two are carried so a status card can show them without a second
    source of truth."""

    model_id: str
    vendor: str
    context_window: int
    max_output: int
    window_1m: bool
    source: str
    source_note: str = ""


@dataclass(frozen=True)
class ContextTable:
    """An immutable snapshot of the table plus where it came from."""

    rows: Mapping[str, ModelContext]
    source: str
    #: Path actually read, or ``None`` when nothing could be read at all.
    path: Optional[Path]
    #: Ids dropped for lacking a citation. Surfaced so the GUI can show them.
    uncited: tuple[str, ...] = ()

    def lookup(self, model_id: str) -> Optional[ModelContext]:
        """EXACT match only. ``glm-5.1-flash-x`` never resolves to ``glm-5.1``."""
        return self.rows.get(model_id)

    def advertise_1m(self, model_id: str) -> bool:
        row = self.lookup(model_id)
        return bool(row and row.window_1m)


def _parse(payload: object, origin: Path) -> tuple[dict[str, ModelContext], tuple[str, ...]]:
    """Parse a table document. Raises ``ValueError`` on a shape it cannot use."""
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
    return rows, tuple(uncited)


class _UnsupportedSchema(ValueError):
    """The document parsed but declares a schema this reader does not know."""


def load_seed() -> ContextTable:
    """The shipped table. A broken seed is a broken BUILD, so it is loud."""
    try:
        payload = json.loads(SEED_PATH.read_text(encoding="utf-8"))
        rows, uncited = _parse(payload, SEED_PATH)
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
            rows, uncited = _parse(payload, self._export_path)
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
        )
        return self._table


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
