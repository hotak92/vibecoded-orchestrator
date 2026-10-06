# Copyright (C) 2026 VibeCoded Tools — AGPL-3.0-or-later
"""Retrieval-event POSTs taken OFF a hook's critical path (v0.2.100 W5R-07).

Why
---
A hook KG search (``rl_kg_search.py --hook-format``) is a short-lived process
whose stdout a hook is waiting for. It used to print its results only AFTER the
primary retrieval event AND the dual-log twin had been POSTed to vct-hub — two
blocking urllib calls of up to 2 s each on a slow or wedged hub. With the 1 s
secondary embed on top, a pre-tool-use run (3 s harness budget) could be
killed before printing anything: no injection, no twin, no ledger line.

What
----
On the hook path ``search_pipeline.rerank_and_emit`` BUILDS the events but does
not send them (``RerankRequest.defer_emit``); it returns them as
:class:`DeferredEmit` items. The producer prints its results first, then calls
:func:`hand_off`, which writes the events to a private temp file and starts a
DETACHED child (this file, run as a script) that sends them and deletes the
file. The hook gets its output as soon as the search is done; the training
events still land. If the child cannot be started, the events are sent inline,
after the output — never dropped.

The PARENT resolves each event's writer (the calling project's identity, as it
always did) and hands its constructor params to the child, which rebuilds the
same writer without importing ``weaviate_mcp.server`` (a light child: ~0.6 s of
imports, off the critical path). Every send still goes through
``telemetry_emit.emit_rl_event`` → ``hub_writer.post_rl_event``, so a failed
POST is recorded in the loss ledger exactly as before.

``VCO_RL_EMIT_INLINE=1`` sends inline (after the output) instead of detaching —
a diagnostic switch for debugging the hub write path in the foreground.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

logger = logging.getLogger(__name__)

_THIS = Path(__file__).resolve()
#: ``<orchestrator>/claude_mcp_servers`` and ``<orchestrator>`` — what the
#: child needs on ``sys.path`` (the same two roots rl_kg_search.py adds).
_MCP_DIR = _THIS.parent.parent
_ORCH_ROOT = _MCP_DIR.parent

INLINE_ENV = "VCO_RL_EMIT_INLINE"


@dataclass(frozen=True)
class DeferredEmit:
    """One built-but-unsent retrieval event.

    ``other_slot`` is ``(embedding_source, embedding_dim, embedding_model)``
    for the dual-log TWIN, whose writer is the other slot's; ``None`` for the
    primary event (the project's default writer).
    """

    event: Any  # telemetry_emit.RetrievalEvent
    other_slot: Optional[tuple] = None
    #: ``RLTelemetryWriter.construction_params()`` of the writer the PARENT
    #: resolved — set only on a batch handed to the child, which rebuilds the
    #: writer from it (same tags, no project re-resolution).
    writer_params: Optional[dict] = None


def _writer_factory_for(item: DeferredEmit):
    if item.writer_params is not None:
        params = dict(item.writer_params)

        def _rebuilt():
            from .telemetry_writer import RLTelemetryWriter

            return RLTelemetryWriter(**params)

        return _rebuilt
    if not item.other_slot:
        return None
    src, dim, model = item.other_slot

    def _factory():
        from claude_mcp_servers.weaviate_mcp.server import _get_rl_telemetry_writer_for

        return _get_rl_telemetry_writer_for(src, embedding_dim=dim, embedding_model=model)

    return _factory


def emit_each(items: Sequence[DeferredEmit], *, emit=None) -> list:
    """Send every item in order (primary first); return one success flag per
    item, positionally. Soft-fail per item; telemetry never raises into a
    search. ``emit`` defaults to ``telemetry_emit.emit_rl_event``.

    A flag is True when the emitter HANDLED the event — which includes a hub
    POST that was ATTEMPTED and failed (``emit_rl_event`` returns True for that
    case; ``hub_writer`` records the failure per event). False means the emitter
    never reached the POST (no writer, a validation error, a raise) — the only
    case with no per-event ledger line of its own.
    """
    from .telemetry_emit import EmitValidationError, emit_rl_event

    send = emit or emit_rl_event
    results = []
    for item in items:
        try:
            results.append(bool(send(item.event, writer_factory=_writer_factory_for(item))))
        except EmitValidationError as exc:
            logger.debug("deferred emit: validation failed (%s)", exc)
            results.append(False)
        except Exception as exc:  # noqa: BLE001 — telemetry never raises into a search
            logger.debug("deferred emit: raised (%s)", exc)
            results.append(False)
    return results


def emit_now(items: Sequence[DeferredEmit], *, emit=None) -> int:
    """Send every item in order (primary first). Soft-fail per item; returns
    the number that reported success (see :func:`emit_each` for the per-item
    contract)."""
    return sum(emit_each(items, emit=emit))


def _can_detach() -> bool:
    """Whether :func:`hand_off` may start a detached child. One seam, so the
    in-process test harness (which captures emits in THIS process) can say no."""
    return os.environ.get(INLINE_ENV, "").strip().lower() not in ("1", "true", "yes", "on")


def _resolve_writer_params(item: DeferredEmit) -> Optional[dict]:
    """The params of the writer THIS process would use for ``item`` (the
    project's default writer, or the other slot's for a twin)."""
    from .telemetry_emit import _default_writer_factory

    factory = _writer_factory_for(item) or _default_writer_factory
    writer = factory()
    if writer is None:
        return None
    return writer.construction_params()


def _serialize(items: Sequence[DeferredEmit]) -> str:
    """The child's batch: each event plus the writer identity resolved HERE.
    An item whose writer cannot be resolved is dropped (``emit_rl_event``
    would have skipped it for the same reason)."""
    batch = []
    for i in items:
        params = _resolve_writer_params(i)
        if params is None:
            logger.debug("deferred emit: no writer for %s; skipped", getattr(i.event, "task_id", "?"))
            continue
        batch.append({"event": dataclasses.asdict(i.event), "writer": params})
    return json.dumps({"items": batch}, separators=(",", ":"))


def _spawn_child(payload: str) -> None:
    """Write ``payload`` to a private temp file and start the detached sender.
    Raises on any failure (the caller then sends inline)."""
    from vco_lib.install_companions import detached_popen_kwargs

    fd, path = tempfile.mkstemp(prefix="vco-rl-emit-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            [str(_ORCH_ROOT), str(_MCP_DIR)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
        )
        subprocess.Popen(  # noqa: S603 — fixed argv: this interpreter + this file
            [sys.executable, str(_THIS), path],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            env=env,
            **detached_popen_kwargs(),
        )
    except BaseException:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise


def hand_off(items: Sequence[DeferredEmit], *, emit=None) -> str:
    """Send ``items`` without holding the caller: ``"detached"`` when a child
    took them, ``"inline"`` when they were sent here (child unavailable or
    :data:`INLINE_ENV` set), ``"none"`` when there was nothing to send.

    Call it AFTER the caller's output is printed and flushed.
    """
    items = [i for i in (items or ()) if i is not None]
    if not items:
        return "none"
    if _can_detach():
        try:
            _spawn_child(_serialize(items))
            return "detached"
        except Exception as exc:  # noqa: BLE001 — fall back to inline, never drop
            logger.debug("deferred emit: detached child unavailable (%s); sending inline", exc)
    emit_now(items, emit=emit)
    return "inline"


def _safe_unlink(path: Any) -> None:
    """Remove ``path`` if present; never raise. One home for the child's
    payload cleanup (the try/finally in :func:`_child_main` and the ``atexit``
    backstop both call it)."""
    try:
        os.unlink(path)
    except OSError:
        pass


def _load(path: str) -> list:
    from .telemetry_emit import RetrievalEvent

    # NB-04: read WITHOUT unlinking — the payload must outlive the emit so a
    # crash between load and POST can still be recorded (and, per NB-05, so the
    # ``finally`` in the caller can be the one true cleanup site).
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    out = []
    for raw in data.get("items") or []:
        ev = RetrievalEvent(**raw["event"])
        out.append(DeferredEmit(ev, None, dict(raw.get("writer") or {})))
    return out


def _record_unsent(
    items: Optional[Sequence["DeferredEmit"]], results: Optional[Sequence[bool]] = None
) -> None:
    """NB-04: ONE loss-ledger line for the events the child never sent.

    Only the items the emitter returned False for have no per-event ledger
    line of their own: an event whose POST was ATTEMPTED and failed is already
    recorded per event by ``hub_writer`` with its own reason
    (``emit_rl_event`` reports True for it), so it is NOT re-counted here. The
    line is kind ``deferred_unsent``—not ``hub_post_failed``—because no POST
    was ever attempted for these events.

    ``results`` is :func:`emit_each`'s per-item flags. ``None`` (a loader crash
    before any emit) means the whole batch is unknown, so all of it is
    reported. Never raises (a loss record must not become a second failure).
    """
    try:
        from vco_lib.rl_telemetry_loss import KIND_DEFERRED_UNSENT, record_loss
    except Exception as exc:  # noqa: BLE001 — a broken import must not crash the child
        logger.debug("deferred emit child: loss ledger unavailable (%s)", exc)
        return
    detail: dict = {}
    if items is not None:
        flags = list(results) if results is not None else []
        flags += [False] * max(0, len(items) - len(flags))
        unsent = [
            getattr(getattr(i, "event", None), "task_id", "?")
            for i, ok in zip(items, flags)
            if not ok
        ]
        detail = {"unsent": len(unsent), "task_ids": ",".join(unsent)}
    try:
        record_loss(KIND_DEFERRED_UNSENT, "deferred_emit_not_sent", **detail)
    except Exception as exc:  # noqa: BLE001 — the loss record must not raise
        logger.debug("deferred emit child: could not record loss (%s)", exc)


def _child_main(argv: Sequence[str]) -> int:
    if len(argv) != 1:
        return 2
    path = argv[0]
    items: Optional[list] = None
    results: Optional[list] = None
    try:
        items = _load(path)              # NB-04: load first, DO NOT unlink yet
        results = emit_each(items)       # NB-04: emit before the payload is removed
        if not all(results):
            _record_unsent(items, results)
        return 0
    except Exception as exc:  # noqa: BLE001 — a crash must still be recorded + cleaned up
        logger.debug("deferred emit child: events not fully sent (%s)", exc)
        _record_unsent(items, results)
        return 1
    finally:
        _safe_unlink(path)               # NB-05: the payload never outlives the child


if __name__ == "__main__":
    # Run as a SCRIPT, so sys.path[0] is this package directory: replace it
    # with the two roots the package imports from (never the caller's cwd,
    # which is a user project that could shadow a module name).
    sys.path[:1] = [str(_ORCH_ROOT), str(_MCP_DIR)]
    # NB-05 best-effort backstop: register the payload cleanup BEFORE the
    # package import, so a crash during that import (or any exit path that
    # bypasses the try/finally) still removes the 0600 temp file. Idempotent
    # with the ``finally`` in :func:`_child_main`.
    if len(sys.argv) == 2:
        import atexit

        atexit.register(_safe_unlink, sys.argv[1])
    from claude_mcp_servers.rl_client.deferred_emit import _child_main as _main

    raise SystemExit(_main(sys.argv[1:]))
