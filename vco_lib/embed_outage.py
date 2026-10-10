# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The ONE loud message for an embedding backend that stays down (v0.2.102).

Owner rulings 2026-10-10: never fall back to a different embedding model (the
vectors would not be comparable); a down backend is retried transiently while
the hub watchdog restarts its container, and if it STAYS down the agent gets a
loud error naming the backend/container, the watchdog state when known, and the
remedy — so Claude can tell the user or fix it. Every surface that hits such an
outage (EmbeddingService, the weaviate-kg MCP tools, the code-graph CLI, the hook
injection) renders this text; none composes its own.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

#: The hub watchdog's give-up deferral (``infra_watchdog.rs`` GaveUpLedger).
WATCHDOG_GAVE_UP_CID = "watchdog_gave_up"
CODE_EMBED_CONTAINER = "vco_code_embed"
OLLAMA_CONTAINER = "vco_ollama"

#: The one-line hook-injection notice (a hook must not print a paragraph).
#: Every such line starts with this prefix; the hook router never caches it.
HOOK_UNAVAILABLE_MARK = "unavailable | [vct]"


def hook_unavailable_line(scheme: str) -> str:
    """The ONE visible line a context-injection hook emits when its search
    could not embed the query (``scheme`` ``"kg"`` or ``"code"``)."""
    if scheme == "code":
        return (f"CODE: {HOOK_UNAVAILABLE_MARK} code graph unavailable: its embedding "
                "backend is down (run search_code_graph for the details and the remedy)")
    return (f"KG: {HOOK_UNAVAILABLE_MARK} KG search unavailable: its embedding backend "
            "is down (run hybrid_search for the details and the remedy)")


def is_hook_unavailable_text(text: str) -> bool:
    """True when a leg's output is (or carries) the outage line — never cache it."""
    return HOOK_UNAVAILABLE_MARK in (text or "")


def _orchestrator_root() -> Path:
    raw = os.environ.get("VCT_ORCHESTRATOR_ROOT", "").strip()
    return Path(raw) if raw else Path(__file__).resolve().parents[1]


def watchdog_state(container: str, root: Optional[Path] = None) -> str:
    """What the hub watchdog says about ``container``: it gave up (its
    ``watchdog_gave_up`` row names the container), or it restarts it."""
    try:
        from vco_lib.deferral_report import DeferralReport

        report = DeferralReport.read(root or _orchestrator_root())
        for entry in report.entries:
            if entry.condition_id == WATCHDOG_GAVE_UP_CID and container in (
                f"{entry.title} {entry.detected}"
            ):
                return (f"the hub watchdog GAVE UP restarting {container} "
                        f"({entry.detected.strip()[:200]})")
    except Exception:  # noqa: BLE001 — an unreadable ledger is "not known"
        return "the hub watchdog state is not known"
    return f"the hub watchdog restarts {container} automatically when it is part of this install"


def _hint(args: str) -> str:
    try:
        from vco_lib.containers import runtime_command_hint

        return runtime_command_hint(args)
    except Exception:  # noqa: BLE001
        return f"podman {args}"


def code_backend_unavailable_message(model: str, code_embed_url: str,
                                     ollama_url: Optional[str] = None) -> str:
    """The loud text for a code-embedding backend that does not answer —
    worded for where ``model`` is SERVED from (vco_lib.code_embed_serving)."""
    from vco_lib.code_embed_serving import (
        CODE_SERVED_BY_CONTAINER,
        CODE_SERVED_BY_OPENAI,
        code_serving_backend,
    )

    serving = code_serving_backend(model)
    if serving == CODE_SERVED_BY_CONTAINER:
        return (f"Code-embedding backend unavailable: the code_embed container "
                f"({CODE_EMBED_CONTAINER}) serving {model} does not answer at {code_embed_url}; "
                f"{watchdog_state(CODE_EMBED_CONTAINER)}. Remedy: wait for the restart, or "
                f"`{_hint('start ' + CODE_EMBED_CONTAINER)}` (logs: "
                f"`{_hint('logs ' + CODE_EMBED_CONTAINER)}`). The code graph is never "
                "queried with a different embedding model.")
    if serving == CODE_SERVED_BY_OPENAI:
        return (f"Code-embedding backend unavailable: OpenAI ({model}) did not answer. "
                "Check the OpenAI key (Preferences → OpenAI API key) and the network; "
                "the code graph is never queried with a different embedding model.")
    where = ollama_url or os.environ.get("OLLAMA_URL", "the configured OLLAMA_URL")
    return (f"Code-embedding backend unavailable: Ollama ({OLLAMA_CONTAINER}) serving "
            f"{model} does not answer at {where} (or lacks the model); "
            f"{watchdog_state(OLLAMA_CONTAINER)}. Remedy: wait for the restart, or "
            f"`{_hint('start ' + OLLAMA_CONTAINER)}`, then `ollama pull {model}` if it is "
            "missing. The code graph is never queried with a different embedding model.")


def _is_openai_text_model(model: str) -> bool:
    """OpenAI per the ONE text slot table (``embedding_service.TEXT_SLOT_MAP``)."""
    from vco_lib.embedding_service import _resolve_text_slot

    return "openai" in _resolve_text_slot(str(model or ""))[0]


def text_backend_unavailable_message(model: str, ollama_url: Optional[str] = None) -> str:
    """The same for the KG text embedder."""
    if _is_openai_text_model(model):
        return (f"KG embedding backend unavailable: OpenAI ({model}) did not answer. "
                "Check the OpenAI key and the network; the KG is never queried with a "
                "different embedding model.")
    where = ollama_url or os.environ.get("OLLAMA_URL", "the configured OLLAMA_URL")
    return (f"KG embedding backend unavailable: Ollama ({OLLAMA_CONTAINER}) serving {model} "
            f"does not answer at {where}; {watchdog_state(OLLAMA_CONTAINER)}. Remedy: "
            f"`{_hint('start ' + OLLAMA_CONTAINER)}`. The KG is never queried with a "
            "different embedding model.")


#: The marker every loud outage text ends with — lets a surface tell "already
#: the loud text" from a raw transport error it must still wrap.
NO_SWITCH_MARK = "never queried with a different embedding model"


class EmbeddingBackendUnavailable(RuntimeError):
    """A search could not embed its query: the backend that serves the
    RECORDED model is down (after the transient retry). Surfaces render
    ``str(exc)`` verbatim as a loud error; ``scheme`` is ``"code"`` or
    ``"kg"``, ``container`` the container to restart (``""`` for OpenAI)."""

    def __init__(self, message: str, *, scheme: str, model: str = "",
                 container: str = "") -> None:
        super().__init__(message)
        self.scheme = scheme
        self.model = model
        self.container = container

    def as_payload(self) -> dict:
        """The structured fields every MCP search tool returns."""
        return {
            "success": False,
            "error_class": "EmbeddingBackendUnavailable",
            "error": str(self),
            "backend_scheme": self.scheme,
            "model": self.model,
            "container": self.container,
        }


def _container_for(scheme: str, model: str) -> str:
    if scheme == "code":
        from vco_lib.code_embed_serving import (
            CODE_SERVED_BY_CONTAINER,
            CODE_SERVED_BY_OPENAI,
            code_serving_backend,
        )

        serving = code_serving_backend(model)
        if serving == CODE_SERVED_BY_CONTAINER:
            return CODE_EMBED_CONTAINER
        return "" if serving == CODE_SERVED_BY_OPENAI else OLLAMA_CONTAINER
    return "" if _is_openai_text_model(model) else OLLAMA_CONTAINER


def query_unavailable(scheme: str, svc=None, exc: Optional[BaseException] = None, *,
                      construction_error: str = "",
                      busy_secs: Optional[float] = None) -> EmbeddingBackendUnavailable:
    """THE one builder of the loud error a search surface returns when its
    query cannot be embedded (MCP tools, the code-graph and KG CLIs, the hook
    router). ``svc`` is the EmbeddingService (None = it could not be built);
    ``exc`` the failure; ``busy_secs`` set when a 503 outlasted the
    interactive bound (busy, not down)."""
    code = scheme == "code"
    graph = "code graph" if code else "KG"
    if svc is None:
        detail = construction_error or (
            "vco_lib.embedding_service is not importable — a broken install")
        return EmbeddingBackendUnavailable(
            f"{'Code' if code else 'KG'} search unavailable: the embedding service could "
            f"not be built ({detail}). Remedy: check `vco doctor` and that the embedding "
            f"containers are running (`{_hint('ps')}`). The {graph} is {NO_SWITCH_MARK}.",
            scheme=scheme,
        )
    model = str(getattr(svc, "code_model_id" if code else "text_model_id", "") or "")
    container = _container_for(scheme, model)
    if busy_secs is not None:
        where = (f"{container or 'OpenAI'} serving {model}" if model
                 else "the embedding backend")
        message = (f"{'Code' if code else 'KG'} embedding backend busy: {where} still "
                   f"answered 503 after {busy_secs:g}s (the interactive bound). Retry "
                   f"shortly; the {graph} is {NO_SWITCH_MARK}.")
    elif exc is not None and NO_SWITCH_MARK in str(exc):
        message = str(exc)
    elif exc is not None and getattr(exc, "permanent", False):
        message = f"{exc} The {graph} is {NO_SWITCH_MARK}."
    else:
        ollama = getattr(svc, "ollama_url", None)
        base = (code_backend_unavailable_message(model, getattr(svc, "code_embed_url", ""), ollama)
                if code else text_backend_unavailable_message(model, ollama))
        message = base + (f" Cause: {exc}" if exc is not None else "")
    return EmbeddingBackendUnavailable(message, scheme=scheme, model=model, container=container)


def embed_query_or_raise(scheme: str, svc, text: str, task: Optional[str] = None, *,
                         construction_error: str = "") -> list:
    """Embed a SEARCH QUERY with the recorded model, or raise the loud error.

    THE one query-embed entry for every search surface (the weaviate-kg MCP
    ``search_code_graph`` / ``hybrid_search`` / ``semantic_graph_search``, the
    code-graph and KG CLIs, the hook router). No surface keeps an inline
    fallback to another endpoint: that endpoint serves another model (owner
    ruling 2026-10-10). Transient arms, all on :mod:`vco_lib.retry_after`:
    a 503 waits up to the interactive bound (then ``busy``); a refused
    connection to Ollama waits for the watchdog's restart on the refused
    bound, then one more try; the CodeSage container's own wait lives in
    ``EmbeddingService`` (``_require_container_serves_code_model``).
    """
    from vco_lib.retry_after import (
        is_503_error,
        is_connection_error,
        reset_503_tolerance_override,
        resolve_503_interactive_tolerance,
        set_503_tolerance_override,
        wait_until_reachable,
    )

    if svc is None:
        raise query_unavailable(scheme, None, construction_error=construction_error)
    code = scheme == "code"
    fn = svc.embed_code_query if code else svc.embed_text_query
    tolerance = resolve_503_interactive_tolerance()
    token = set_503_tolerance_override(tolerance)
    try:
        try:
            vec = fn(text, task)
        except Exception as exc:  # noqa: BLE001 — classified below
            if is_503_error(exc):
                raise query_unavailable(scheme, svc, exc, busy_secs=tolerance) from exc
            container = _container_for(
                scheme, str(getattr(svc, "code_model_id" if code else "text_model_id", "") or ""))
            ollama = getattr(svc, "ollama", None)
            if not (is_connection_error(exc) and container == OLLAMA_CONTAINER
                    and ollama is not None
                    and wait_until_reachable(ollama.is_reachable, interactive=True)):
                raise query_unavailable(scheme, svc, exc) from exc
            try:
                vec = fn(text, task)
            except Exception as exc2:  # noqa: BLE001
                raise query_unavailable(scheme, svc, exc2) from exc2
    finally:
        reset_503_tolerance_override(token)
    if not vec:
        raise query_unavailable(scheme, svc)
    return vec
