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


def emit_now(items: Sequence[DeferredEmit], *, emit=None) -> int:
    """Send every item in order (primary first). Soft-fail per item; returns
    the number that reported success. ``emit`` defaults to
    ``telemetry_emit.emit_rl_event`` (``search_pipeline`` passes its own
    binding, so there is one emitter per process to observe)."""
    from .telemetry_emit import EmitValidationError, emit_rl_event

    send = emit or emit_rl_event
    ok = 0
    for item in items:
        try:
            if send(item.event, writer_factory=_writer_factory_for(item)):
                ok += 1
        except EmitValidationError as exc:
            logger.debug("deferred emit: validation failed (%s)", exc)
        except Exception as exc:  # noqa: BLE001 — telemetry never raises into a search
            logger.debug("deferred emit: raised (%s)", exc)
    return ok


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


def _load(path: str) -> list:
    from .telemetry_emit import RetrievalEvent

    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    out = []
    for raw in data.get("items") or []:
        ev = RetrievalEvent(**raw["event"])
        out.append(DeferredEmit(ev, None, dict(raw.get("writer") or {})))
    return out


def _child_main(argv: Sequence[str]) -> int:
    if len(argv) != 1:
        return 2
    emit_now(_load(argv[0]))
    return 0


if __name__ == "__main__":
    # Run as a SCRIPT, so sys.path[0] is this package directory: replace it
    # with the two roots the package imports from (never the caller's cwd,
    # which is a user project that could shadow a module name).
    sys.path[:1] = [str(_ORCH_ROOT), str(_MCP_DIR)]
    from claude_mcp_servers.rl_client.deferred_emit import _child_main as _main

    raise SystemExit(_main(sys.argv[1:]))
