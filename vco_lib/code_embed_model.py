# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Which code-embedding model a project's code graph uses — the ONE resolver.

v0.2.102 W6-B2. Every consumer that embeds or searches code — the analyzer,
the weaviate-kg MCP, ``code-graph-query``, the hook router's code leg, the
slot enrichment — constructs :meth:`vco_lib.embedding_service.EmbeddingService.for_project`,
and ``for_project`` asks THIS module. Nothing else decides the code model.

Two questions, kept apart on purpose:

* the **target** — what the configuration selects (:func:`resolve_code_model_pick`);
* the **record** — what the graph's COMPLETE slot holds, kept in the
  launcher's existing ``project_codegraph_bindings.embedding_model`` /
  ``embedding_dim`` (the row the launcher's build path stamps from the
  analyzer's ``CODEGRAPH_PROVENANCE`` line since v0.2.82).

Owner ruling 2026-10-10 — a selection is APPLIED, automatically and additively:
when the target differs from the record, the ordinary paths (every analyzer run
that is not a single-file edit, the incremental sync, the launcher build, the
bundle update's self-heal) add the target's named vector to the collections
(:func:`vco_lib.weaviate_schema.add_named_vector_slot`, vector-preserving) and
the walk embeds the rows that lack it (the P299-B5 vectorless veto under
force-rewalk) while the old vectors stay. Searches keep the recorded model until
the target slot is COMPLETE (every row has it); then the record flips. Switching
back to a model whose slot is still complete is an instant flip. Existing
vectors are re-embedded only when the existing generation metadata says they
are stale (``embed_revision`` / content hash / the extractor-generation
ladder) — no second versioning scheme. Fill progress is kept on the same
binding row (``config_json.code_model_fill``) for the Preferences status.

Target precedence (first hit wins):

1. ``CODE_EMBED_MODEL`` env — the operator's explicit model.
1b. the project's own selection (Codegraph tab → ``config_json.code_model_target``).
2. the launcher Preferences pin (``code_embed_backend_override`` + its model key).
3. the install default — ``app_state[default_code_embedding]`` (install.py's
   hardware ladder; the "Default code embedding" dropdown).
3b. the install RECORD — the code model of the profile install wrote at step 7
   (``<orchestrator>/.claude/state/ollama_pull_profile.json``, the same value
   the pull plan and compose derive from; v0.2.103 F1).
4. ``CODE_EMBED_BACKEND`` env — the code_embed CONTAINER's selector, which
   install.py projects into the root's settings env: a default only, never an
   override (review HIGH-1). ``openai`` keeps meaning "the OpenAI default
   model"; ``gpu`` is honoured ONLY when the hardware ladder agrees this host
   should run CodeSage; ``ollama`` names a backend, not a model, and is
   ignored (v0.2.103 F1 removed the fixed ``ollama → qwen3`` mapping — on a
   Jina-tier host it named a model install never pulled).
5. the hardware ladder itself —
   :func:`vco_lib.embedding_selection.select_code_model_for_host` over the
   live probes (:mod:`vco_lib.host_probe`), i.e. the SAME rule install's
   profile choice uses. The hardware-blind ``codesage-large-v2`` builtin
   default is gone (v0.2.103 F1): CodeSage is never selected below the
   ladder's 12 GB VRAM threshold.

OpenAI code models need the EMBEDDINGS consent (:mod:`vco_lib.openai_consent`,
shared with the KG text OpenAI slots; separate from the summaries consent).
Without it a target is not filled and the record keeps serving searches.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Sequence

from vco_lib.embedding_service import (
    _FALLBACK_JINA_MODEL,
    DEFAULT_CODE_MODEL,
    DEFAULT_CODE_SLOT,
    DEFAULT_TEXT_MODEL,
    OpenAIEmbeddingsConsentRefusal,
    _resolve_code_slot,
    _to_openai_api_model,
)

#: Written by the launcher's Preferences page (``preferences/loaders.ts``
#: ``APP_STATE_KEY_CODE_EMBED_*`` — MUST MATCH those constants).
APP_STATE_KEY_CODE_EMBED_OVERRIDE = "code_embed_backend_override"
APP_STATE_KEY_CODE_EMBED_OPENAI_MODEL = "code_embed_openai_model"
APP_STATE_KEY_CODE_EMBED_OLLAMA_MODEL = "code_embed_ollama_model"
#: install.py's hardware-ladder pick / the "Default code embedding" dropdown.
from vco_lib.code_embed_serving import APP_STATE_KEY_DEFAULT_CODE_EMBED  # noqa: E402,F401

SOURCE_ENV = "env"
SOURCE_PROJECT = "project"
SOURCE_PREFERENCES = "preferences"
SOURCE_DEFAULT = "default"
SOURCE_INSTALL_RECORD = "install-record"
SOURCE_INSTALL_ENV = "install-env"
SOURCE_RECORDED = "recorded"
SOURCE_HARDWARE = "hardware-probe"

#: The binding row's ``config_json`` key holding the fill progress.
FILL_CONFIG_KEY = "code_model_fill"
#: The binding row's ``config_json`` key holding a PER-PROJECT selection (the
#: project page's Codegraph tab) — MUST MATCH ``kg-bindings.ts``.
TARGET_CONFIG_KEY = "code_model_target"

_OPENAI_CODE_EMBED_DEFAULT_MODEL = "text-embedding-3-small"
_CODE_BASENAMES = ("CodeFunction", "CodeClass", "CodeModule", "CodeAPI", "CodeInteraction")
#: The analyzer's exit code when the RECORDED model's slot is not declared on
#: an existing collection, or the only model it could use is refused for
#: OpenAI consent (distinct from 1-5).
EXIT_CODE_MODEL_REFUSED = 6

#: Canonical model per code slot, for reading a record off live data. The
#: legacy ``ollama_code_embed`` / ``openai_embed`` slots are ambiguous (several
#: models wrote them) and are deliberately absent: no guess.
_SLOT_MODEL = {
    "codesage_embed": DEFAULT_CODE_MODEL,
    "qwen3_embed": DEFAULT_TEXT_MODEL,
    "jina_embed": _FALLBACK_JINA_MODEL,
}


@dataclass(frozen=True)
class CodeModelRecord:
    """What a project's code graph was indexed with."""

    model: str
    dim: int
    slot: str


@dataclass(frozen=True)
class CodeModelResolution:
    """The resolver's whole answer for one graph and one ROLE.

    ``model`` is what this role embeds with: the recorded model for searches
    (``role="query"``), the target for writes (``role="write"``). ``filling``
    — the target differs from the record and is being filled; ``carry`` — the
    model a write also embeds with during the fill (the recorded one, so the
    slot searches read stays complete)."""

    model: str
    source: str
    target: str
    target_source: str
    recorded: Optional[CodeModelRecord]
    filling: bool
    role: str = "query"
    carry: Optional[str] = None
    target_refused: bool = False
    refusal: Optional[OpenAIEmbeddingsConsentRefusal] = None

    @property
    def record_refused(self) -> bool:
        return bool(self.recorded is not None and is_openai_code_model(self.recorded.model)
                    and self.refusal is not None and self.model == self.recorded.model)

    @property
    def slot(self) -> str:
        return code_model_slot(self.model)[0]


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _app_state_reader() -> Callable[[str], Optional[str]]:
    from vco_lib.launcher_db_reader import read_app_state_value

    return read_app_state_value


def _norm_model(model: str) -> str:
    m = _to_openai_api_model(str(model or "").strip().lower())
    if m.endswith(":latest"):
        m = m[: -len(":latest")]
    return m.rsplit("/", 1)[-1]


def code_model_slot(model: str) -> "tuple[str, int]":
    """``(slot, dim)`` for a code model — the ONE slot map
    (``embedding_service._resolve_code_slot``) plus the known Ollama dims for a
    model that falls into the generic ``ollama_code_embed`` slot."""
    slot, dim = _resolve_code_slot(_to_openai_api_model(model or ""))
    if slot == DEFAULT_CODE_SLOT[0]:
        try:
            from vco_lib.embedding_providers.ollama import KNOWN_OLLAMA_DIMS

            dim = int(KNOWN_OLLAMA_DIMS.get(model, dim))
        except Exception:  # noqa: BLE001 — the catalog dim is the fallback
            pass
    return slot, dim


def same_code_model(a: str, b: str) -> bool:
    """True when two model ids embed into the same vector space: same slot
    and dim, and — in the generic ``ollama_code_embed`` slot, which several
    models share — the same model name."""
    sa, da = code_model_slot(a)
    sb, db = code_model_slot(b)
    if (sa, da) != (sb, db):
        return False
    return sa != DEFAULT_CODE_SLOT[0] or _norm_model(a) == _norm_model(b)


def is_openai_code_model(model: str) -> bool:
    from vco_lib.code_embed_serving import is_openai_code_model as _is_openai

    return _is_openai(model)


def _str(app_state: Callable[[str], Optional[str]], key: str) -> str:
    raw = app_state(key)
    return raw.strip() if isinstance(raw, str) else ""


def _pin_model(pin: str, app_state) -> Optional[str]:
    if pin == "codesage":
        return DEFAULT_CODE_MODEL
    if pin == "qwen3":
        return DEFAULT_TEXT_MODEL
    if pin == "jina":
        return _FALLBACK_JINA_MODEL
    if pin == "openai":
        return _to_openai_api_model(
            _str(app_state, APP_STATE_KEY_CODE_EMBED_OPENAI_MODEL)
            or _OPENAI_CODE_EMBED_DEFAULT_MODEL
        )
    if pin == "ollama":
        model = _str(app_state, APP_STATE_KEY_CODE_EMBED_OLLAMA_MODEL)
        if model:
            return model
        print(
            "[vct] code-embed: Preferences pin is 'ollama' but no model is picked "
            f"({APP_STATE_KEY_CODE_EMBED_OLLAMA_MODEL} empty); ignoring the pin",
            file=sys.stderr,
        )
        return None
    print(
        f"[vct] code-embed: ignoring unknown Preferences pin {pin!r} "
        f"({APP_STATE_KEY_CODE_EMBED_OVERRIDE})",
        file=sys.stderr,
    )
    return None


def _orchestrator_roots() -> "list":
    """Where the install record may live — the SAME ladder
    :mod:`vco_lib.summary_model` uses (``VCT_INSTALL_ROOT``, then the
    checkout this ``vco_lib`` is imported from). Imported, not re-declared,
    so the two resolvers can never discover different roots."""
    from vco_lib.summary_model import _orchestrator_roots as _roots

    return list(_roots())


def recorded_code_model(roots: Optional[Sequence] = None) -> Optional[str]:
    """The code model of the profile install recorded (and pulled), or None.

    This is the same value install.py, the pull plan and compose all derive
    from — reading it here makes the resolver agree with what the machine
    actually runs even in processes that carry none of the settings env
    (the 5b bundle child, the hub- or retry-driven walks)."""
    from vco_lib.embedding_pull_plan import read_record

    for root in (roots if roots is not None else _orchestrator_roots()):
        rec = read_record(root)
        model = rec.get("code_model") if isinstance(rec, dict) else None
        if isinstance(model, str) and model.strip():
            return model.strip()
    return None


def _gpu_vendor() -> str:
    """``"nvidia"`` / ``"amd"`` / ``""`` — the explicit ``VCT_GPU_VENDOR``
    preference, else the first attributed card, else unknown. The AMD rule
    needs it: CodeSage has no ROCm build, so an AMD host on the >= 12 GB
    rung runs qwen3 instead."""
    try:
        from vco_lib.gpu_profile import gpu_vendor_preference_from_env

        pref = gpu_vendor_preference_from_env()
        if pref:
            return pref
    except Exception:  # noqa: BLE001 — a failed env read never blocks
        pass
    try:
        from vco_lib import gpu_device

        for cand in gpu_device.enumerate_gpus():
            vendor = (getattr(cand, "vendor", "") or "").strip().lower()
            if vendor in ("nvidia", "amd"):
                return vendor
    except Exception:  # noqa: BLE001 — soft-fail to "unknown"
        pass
    return ""


def _hardware_code_model() -> str:
    """The ladder's answer for THIS host, right now — the ONE selector
    install's own profile choice uses
    (:func:`vco_lib.embedding_selection.select_code_model_for_host`), fed by
    the ONE probe module (:mod:`vco_lib.host_probe`). Reached only when
    nothing configured and no install record answers."""
    from vco_lib.embedding_selection import select_code_model_for_host
    from vco_lib import host_probe

    return select_code_model_for_host(
        gpu_vram_gb=host_probe.probe_gpu_vram_gb(),
        ram_gb=host_probe.probe_system_ram_gb(),
        cores=host_probe.probe_cpu_cores(),
        gpu_vendor=_gpu_vendor(),
    )


def resolve_code_model_pick(
    env: Optional[Mapping[str, str]] = None,
    *,
    app_state: Optional[Callable[[str], Optional[str]]] = None,
    project_target: Optional[str] = None,
    install_record: Optional[Callable[[], Optional[str]]] = None,
    hardware: Optional[Callable[[], str]] = None,
) -> "tuple[str, str]":
    """``(model, source)`` the configuration asks for — see the module
    docstring for the precedence. ``install_record`` / ``hardware`` are
    seams for tests; production passes neither. Never raises; the consent
    gate is applied by :func:`resolve_code_model` to whatever model ends up
    effective."""
    env = os.environ if env is None else env
    app_state = app_state or _app_state_reader()

    env_model = str(env.get("CODE_EMBED_MODEL", "") or "").strip()
    if env_model:
        return env_model, SOURCE_ENV
    if project_target and project_target.strip():
        return _to_openai_api_model(project_target.strip()), SOURCE_PROJECT
    pin = _str(app_state, APP_STATE_KEY_CODE_EMBED_OVERRIDE).lower()
    if pin not in ("", "auto"):
        model = _pin_model(pin, app_state)
        if model:
            return model, SOURCE_PREFERENCES
    default = _str(app_state, APP_STATE_KEY_DEFAULT_CODE_EMBED)
    if default:
        return _to_openai_api_model(default), SOURCE_DEFAULT
    recorded = (install_record or recorded_code_model)()
    if recorded:
        return _to_openai_api_model(recorded), SOURCE_INSTALL_RECORD
    backend = str(env.get("CODE_EMBED_BACKEND", "") or "").strip().lower()
    if backend == "openai":
        return _OPENAI_CODE_EMBED_DEFAULT_MODEL, SOURCE_INSTALL_ENV
    ladder = (hardware or _hardware_code_model)()
    if backend == "gpu" and ladder != DEFAULT_CODE_MODEL:
        # v0.2.103 F1: the projected GPU-service selector is honoured ONLY
        # when the ladder agrees this host should run CodeSage — never below
        # the 12 GB VRAM threshold (a hardware-blind codesage here names a
        # container this host does not run).
        print(
            f"[vct] code-embed: ignoring CODE_EMBED_BACKEND=gpu — the hardware "
            f"ladder selects {ladder} for this host",
            file=sys.stderr,
        )
        return ladder, SOURCE_HARDWARE
    if backend == "gpu":
        return DEFAULT_CODE_MODEL, SOURCE_INSTALL_ENV
    return ladder, SOURCE_HARDWARE


_SOURCE_LABEL = {
    SOURCE_PROJECT: "this project's Codegraph-tab selection",
    SOURCE_PREFERENCES: f"the Preferences pin ({APP_STATE_KEY_CODE_EMBED_OVERRIDE})",
    SOURCE_DEFAULT: f"the install default ({APP_STATE_KEY_DEFAULT_CODE_EMBED})",
    SOURCE_INSTALL_RECORD: "the recorded install profile (ollama_pull_profile.json)",
    SOURCE_INSTALL_ENV: "the install-projected CODE_EMBED_BACKEND",
    SOURCE_HARDWARE: "the hardware ladder (vco_lib.embedding_selection)",
    SOURCE_RECORDED: "this project's recorded code-graph model",
}


def consent_refusal_for(
    model: str, source: str, app_state,
) -> Optional[OpenAIEmbeddingsConsentRefusal]:
    """The refusal for an OpenAI code model without the EMBEDDINGS consent
    (:mod:`vco_lib.openai_consent`, the ONE reader), else None."""
    from vco_lib.openai_consent import embeddings_consent_granted, refusal_message

    if not is_openai_code_model(model) or embeddings_consent_granted(app_state):
        return None
    return OpenAIEmbeddingsConsentRefusal(
        refusal_message(model, f"the code graph ({_SOURCE_LABEL.get(source, source)})"),
        model,
    )


# ---------------------------------------------------------------------------
# The record (launcher.db project_codegraph_bindings)
# ---------------------------------------------------------------------------


def canonical_prefix(name: str) -> str:
    from vco_lib.codegraph_naming import canonical_class_prefix

    try:
        return canonical_class_prefix(name)
    except Exception:  # noqa: BLE001 — an unsanitisable name has no record
        return ""


def record_from_binding(row: Optional[Mapping[str, Any]]) -> Optional[CodeModelRecord]:
    """A binding row is a RECORD only when a build stamped it
    (``last_analyzed_at``). The seed every binding starts with (a hardcoded
    codesage/2048, never built) is not. A per-project SELECTION is a target
    (``config.code_model_target``), never a record — the record moves only when
    a slot is complete."""
    if not row:
        return None
    model = str(row.get("embedding_model") or "").strip()
    if not model:
        return None
    if row.get("last_analyzed_at") is None:
        return None
    slot, dim = code_model_slot(model)
    try:
        stored_dim = int(row.get("embedding_dim") or 0)
    except (TypeError, ValueError):
        stored_dim = 0
    return CodeModelRecord(model=model, dim=stored_dim or dim, slot=slot)


def target_from_binding(row: Optional[Mapping[str, Any]]) -> Optional[str]:
    config = (row or {}).get("config") or {}
    target = config.get(TARGET_CONFIG_KEY) if isinstance(config, dict) else None
    return target.strip() if isinstance(target, str) and target.strip() else None


def read_binding_row(codegraph_prefix: str) -> Optional[Mapping[str, Any]]:
    from vco_lib.launcher_db_reader import get_codegraph_binding_row

    prefix = canonical_prefix(codegraph_prefix) if codegraph_prefix else ""
    return get_codegraph_binding_row(prefix) if prefix else None


def read_code_model_record(codegraph_prefix: str) -> Optional[CodeModelRecord]:
    return record_from_binding(read_binding_row(codegraph_prefix))


def write_code_model_record(codegraph_prefix: str, record: CodeModelRecord) -> bool:
    """Stamp the record onto the project's existing binding row (soft-fail)."""
    from vco_lib.kg_binding_heal import stamp_codegraph_model_record
    from vco_lib.launcher_db_reader import _discover_db_path

    db_path = _discover_db_path()
    prefix = canonical_prefix(codegraph_prefix)
    if db_path is None or not prefix:
        return False
    return stamp_codegraph_model_record(
        db_path, prefix, record.model, record.dim, int(time.time() * 1000),
    )


def resolve_code_model(
    *,
    codegraph_prefix: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
    app_state: Optional[Callable[[str], Optional[str]]] = None,
    record: Optional[CodeModelRecord] = None,
    record_reader: Optional[Callable[[str], Optional[CodeModelRecord]]] = None,
    role: str = "query",
    project_target: Optional[str] = None,
    install_record: Optional[Callable[[], Optional[str]]] = None,
    hardware: Optional[Callable[[], str]] = None,
) -> CodeModelResolution:
    """Which code model one project's code graph uses, for ``role``.

    * no record yet → the target, for both roles (the first run records it);
    * record == target → the record;
    * record != target → searches keep the record, writes embed the target and
      carry the record (the additive fill); a target the EMBEDDINGS consent
      refuses is NOT filled — writes keep the record, and the refusal is
      reported (``target_refused``), so nothing pays and nothing breaks.

    ``codegraph_prefix`` defaults to ``CODE_GRAPH_PROJECT`` (the binding
    prefix every project's MCP env carries); ``record`` supersedes the
    launcher.db read.
    """
    env = os.environ if env is None else env
    app_state = app_state or _app_state_reader()
    if record is None:
        prefix = codegraph_prefix
        if prefix is None:
            prefix = str(env.get("CODE_GRAPH_PROJECT", "") or "").strip()
        try:
            if record_reader is not None:
                record = record_reader(prefix) if prefix else None
            else:
                row = read_binding_row(prefix) if prefix else None
                record = record_from_binding(row)
                project_target = project_target or target_from_binding(row)
        except Exception:  # noqa: BLE001 — an unreadable record is "none"
            record = None
    target, target_source = resolve_code_model_pick(
        env, app_state=app_state, project_target=project_target,
        install_record=install_record, hardware=hardware)
    target_refusal = consent_refusal_for(target, target_source, app_state)
    filling, carry = False, None
    if record is None:
        model, source = target, target_source
    elif same_code_model(target, record.model) or role == "query" or target_refusal:
        model, source = record.model, SOURCE_RECORDED
        filling = not same_code_model(target, record.model) and target_refusal is None
    elif is_openai_code_model(record.model) and consent_refusal_for(
            record.model, SOURCE_RECORDED, app_state) is not None:
        # Re-review MEDIUM (a): an OpenAI-RECORDED graph cannot keep its
        # vectors fresh without the embeddings consent, and a fill would strip
        # them from every rewritten row — so no fill starts: the record stays
        # and its own (refused) model is what this role gets — loud.
        model, source = record.model, SOURCE_RECORDED
    else:  # a write while the target fills; the record's vector rides along
        model, source, filling = target, target_source, True
        carry = record.model
    refusal = consent_refusal_for(model, source, app_state)
    return CodeModelResolution(
        model=model, source=source, target=target, target_source=target_source,
        recorded=record, filling=filling, role=role, carry=carry,
        target_refused=target_refusal is not None, refusal=refusal,
    )


# ---------------------------------------------------------------------------
# Live Weaviate (REST, soft-fail) — the record off stored data, slot checks,
# object counts, the additive slot-add for the fill
# ---------------------------------------------------------------------------


def _collections(prefix: str) -> "list[str]":
    return [f"{prefix}_{b}" for b in _CODE_BASENAMES]


def _declared_slots(name: str, weaviate_url: Optional[str]) -> Optional[set]:
    from vco_lib.embed_slot_fill import declared_slots

    return declared_slots(name, weaviate_url)


#: How many stored objects per collection a live record read samples (review
#: F4: one object cannot tell a complete slot from a partly-filled one).
_LIVE_SAMPLE = 20


def _sample_vectors(name: str, weaviate_url: Optional[str]) -> "dict[str, int]":
    """``{slot: dim}`` for the named vectors populated on EVERY sampled stored
    object (up to :data:`_LIVE_SAMPLE`) — the shared core
    (:func:`vco_lib.embed_slot_fill.sample_common_vectors`, W6-I)."""
    from vco_lib.embed_slot_fill import sample_common_vectors

    return sample_common_vectors(name, weaviate_url, _LIVE_SAMPLE)


def _object_count(name: str, weaviate_url: Optional[str]) -> int:
    from vco_lib.embed_slot_fill import object_count

    return object_count(name, weaviate_url)


def record_from_live(
    codegraph_prefix: str, pick: str, *, weaviate_url: Optional[str] = None,
) -> Optional[CodeModelRecord]:
    """Read what an existing code graph holds off one stored object per
    collection. The pick wins when its slot is populated at its dim;
    otherwise the first unambiguous populated slot. None when nothing is
    stored, nothing is unambiguous, or Weaviate is unreachable."""
    prefix = canonical_prefix(codegraph_prefix)
    pick_slot, pick_dim = code_model_slot(pick)
    # Review F4: a fill toward the pick is in progress → its slot is partial
    # by definition; never short-circuit on it (another populated slot is the
    # record, or nothing is).
    fill = fill_from_binding(read_binding_row(prefix)) or {}
    pick_filling = bool(fill.get("target")) and same_code_model(str(fill["target"]), pick)
    try:
        for name in _collections(prefix):
            populated = _sample_vectors(name, weaviate_url)
            if not populated:
                continue
            if not pick_filling and populated.get(pick_slot) == pick_dim:
                return CodeModelRecord(pick, pick_dim, pick_slot)
            for slot, model in _SLOT_MODEL.items():
                if slot in populated and not (pick_filling and slot == pick_slot):
                    return CodeModelRecord(model, populated[slot], slot)
            if "openai_code_embed" in populated:
                dim = populated["openai_code_embed"]
                model = "text-embedding-3-large" if dim == 3072 else "text-embedding-3-small"
                return CodeModelRecord(model, dim, "openai_code_embed")
    except Exception:  # noqa: BLE001 — unreachable/unreadable → no record
        return None
    return None


def count_code_objects(codegraph_prefix: str, *, weaviate_url: Optional[str] = None) -> Optional[int]:
    """Objects across the project's five code collections (what an apply
    fill embeds at most); None when Weaviate cannot be asked."""
    prefix = canonical_prefix(codegraph_prefix)
    try:
        return sum(_object_count(n, weaviate_url) for n in _collections(prefix))
    except Exception:  # noqa: BLE001
        return None


def ensure_slot_on_collections(
    codegraph_prefix: str, model: str, *, add: bool, weaviate_url: Optional[str] = None,
    slot_adder: Optional[Callable[..., Any]] = None,
) -> Optional[str]:
    """Check (``add=False``) or make (``add=True``) every EXISTING code
    collection declare ``model``'s slot. Returns an error message, or None.

    The shared core (:func:`vco_lib.embed_slot_fill.ensure_slot`, W6-I — the
    KG fill uses the same one): the add is the additive, vector-preserving
    slot-add; an unreachable Weaviate gives no verdict (the walk's connect
    reports it); a collection that does not exist yet needs no slot."""
    from vco_lib.embed_slot_fill import ensure_slot
    from vco_lib.weaviate_schema import CODE_NAMED_VECTORS

    slot, dim = code_model_slot(model)
    return ensure_slot(
        _collections(canonical_prefix(codegraph_prefix)), slot, dim, CODE_NAMED_VECTORS,
        model=model, add=add, weaviate_url=weaviate_url, slot_adder=slot_adder,
        declared=lambda name, url: _declared_slots(name, url),
    )


# ---------------------------------------------------------------------------
# Fill state (the binding row) + the completeness scan
# ---------------------------------------------------------------------------


def fill_from_binding(row: Optional[Mapping[str, Any]]) -> Optional[dict]:
    config = (row or {}).get("config") or {}
    fill = config.get(FILL_CONFIG_KEY) if isinstance(config, dict) else None
    return fill if isinstance(fill, dict) else None


def _db_and_prefix(codegraph_prefix: str):
    from vco_lib.launcher_db_reader import _discover_db_path

    return _discover_db_path(), canonical_prefix(codegraph_prefix)


def write_fill_progress(codegraph_prefix: str, target: str, filled: int, total: int,
                        *, revision: Optional[int] = None, source: str = "scan") -> bool:
    """Stamp ``{target, filled, total, revision, source}`` on the binding row —
    ``source`` is ``walk`` (the fill walk's own counters) or ``scan``."""
    from vco_lib.kg_binding_heal import stamp_codegraph_model_fill

    db_path, prefix = _db_and_prefix(codegraph_prefix)
    if db_path is None or not prefix:
        return False
    return stamp_codegraph_model_fill(db_path, prefix, {
        "target": target, "filled": int(filled), "total": int(total),
        "revision": revision, "source": source, "at": int(time.time() * 1000)})


def current_embed_revision() -> Optional[int]:
    """The code graph's current ``embed_revision`` (the analyzer constant the
    resync driver already resolves) — a stamped fill counter is trusted only
    at the revision it was counted at."""
    try:
        from pathlib import Path

        from vco_lib.codegraph_resync import _resolve_embed_revision

        analyzer = Path(__file__).resolve().parents[1] / "templates" / "scripts" / "analyze_code_graph.py"
        return int(_resolve_embed_revision(analyzer)) if analyzer.is_file() else None
    except Exception:  # noqa: BLE001
        return None


def _default_client():
    from vco_lib import weaviate_helpers as _wh

    return _wh.connect_v4()


def scan_slot_fill(
    codegraph_prefix: str, slot: str, *, client_factory: Optional[Callable[[], Any]] = None,
) -> "Optional[tuple[int, int]]":
    """``(filled, total)`` across the graph's existing code collections:
    objects whose ``slot`` holds a vector vs all objects — the shared cursor
    scan (:func:`vco_lib.embed_slot_fill.scan_slot_counts`, W6-I). None when
    Weaviate cannot be asked (never a guess)."""
    from vco_lib.embed_slot_fill import scan_slot_counts

    measured = scan_slot_counts(_collections(canonical_prefix(codegraph_prefix)), [slot],
                                client_factory=client_factory or _default_client)
    if measured is None:
        return None
    counts, total = measured
    return counts[slot], total


def _counters_consistent(counters: Optional[Mapping[str, Any]], target: str,
                         total: Optional[int], revision: Optional[int]) -> bool:
    """Trust stamped/walk counters only when they are about THIS target, at
    THIS revision, and add up against the graph's CURRENT object count.

    W6-B2 final round (review F1 HIGH): a clean whole-graph walk's counters
    cover exactly the rows the walk is RESPONSIBLE for — every row of a file
    it walked. Orphan rows (a deleted file, a refactored-away entity: no walk
    visits them, and the resync driver never prunes) are not, so for
    ``source == "walk"`` the live count may EXCEED the counted total; any
    other source must match it exactly."""
    if not counters or total is None:
        return False
    try:
        filled, counted_total = int(counters["filled"]), int(counters["total"])
    except (KeyError, TypeError, ValueError):
        return False
    within = (counted_total <= total if counters.get("source") == "walk"
              else counted_total == total)
    return (same_code_model(str(counters.get("target") or ""), target)
            and within and 0 <= filled <= counted_total
            and (revision is None or counters.get("revision") in (None, revision)))


def settle_fill(
    codegraph_prefix: str, target: str, *, counters: Optional[Mapping[str, Any]] = None,
    client_factory: Optional[Callable[[], Any]] = None,
    weaviate_url: Optional[str] = None,
) -> Optional[str]:
    """Settle the target's slot: COMPLETE → flip the record to the target
    (searches move to it; ``"complete"``); otherwise stamp the progress
    (``"filling"``). None when it cannot be measured.

    Re-review MEDIUM (b): completeness comes from ``counters`` — the fill
    walk's own tally, or the progress stamped on the binding row — checked
    against the graph's current object count (five cheap aggregates). The full
    vector scan runs ONLY when counters are missing or inconsistent."""
    slot, dim = code_model_slot(target)
    revision = current_embed_revision()
    total = count_code_objects(codegraph_prefix, weaviate_url=weaviate_url)
    if counters is not None and total is not None and _counters_consistent(
            counters, target, total, revision):
        filled, total = int(counters["filled"]), int(counters["total"])
        source = str(counters.get("source") or "walk")
    else:
        measured = scan_slot_fill(codegraph_prefix, slot, client_factory=client_factory)
        if measured is None:
            return None
        (filled, total), source = measured, "scan"
    if total <= 0:
        # Nothing measured (an empty graph, or a walk that read no row): no
        # flip onto a slot no object proves (W6-B2 final round, LOW).
        return None
    if filled == total:
        write_code_model_record(codegraph_prefix, CodeModelRecord(target, dim, slot))
        return "complete"
    write_fill_progress(codegraph_prefix, target, filled, total, revision=revision, source=source)
    return "filling"


# ---------------------------------------------------------------------------
# The analyzer's two seams (kept here so analyze_code_graph.py stays thin)
# ---------------------------------------------------------------------------


@dataclass
class AnalyzerRunPlan:
    """What one analyzer run embeds with, and how it settles afterwards."""

    record: Optional[CodeModelRecord]
    model: str
    role: str = "write"
    target: str = ""
    filling: bool = False
    rewalk: bool = False
    force_recreate: bool = False
    refusal: str = ""


def _fill_line(prefix: str, res: CodeModelResolution, weaviate_url: Optional[str]) -> str:
    n = count_code_objects(prefix, weaviate_url=weaviate_url)
    cost = " through the OpenAI API (cost-bearing)" if is_openai_code_model(res.target) else ""
    if res.carry and is_openai_code_model(res.carry):
        cost += (f"; rows rewritten meanwhile also refresh their {res.carry} vector "
                 "through the OpenAI API (cost-bearing)")
    return (f"[vct] code-embed: {prefix} moves to {res.target} (slot {code_model_slot(res.target)[0]}, "
            f"from {_SOURCE_LABEL.get(res.target_source, res.target_source)}); filling up to "
            f"{n if n is not None else '?'} object(s){cost} — existing vectors are kept, "
            f"searches stay on {res.recorded.model if res.recorded else '?'} until the slot is complete.")


def prepare_analyzer_run(
    codegraph_prefix: str, *, scoped: bool, force_recreate: bool,
    weaviate_url: Optional[str] = None,
) -> AnalyzerRunPlan:
    """Before the walk: which model this run embeds with.

    * ``--force-recreate`` → the target (the collections start empty).
    * no record → one is read off live data and stamped (graphs indexed before
      v0.2.102 — no re-embed); still none → the target (first build).
    * the RECORDED model's slot missing from an existing collection → refused
      (exit 6), never a run whose writes 422 while it reports success; with no
      record the target's slot is added (additive) before the first build.
    * target ≠ record → the fill: the target's named vector is added
      (additive) and the run embeds the target (+ carries the record) with
      force-rewalk unless it is a scoped single-file run. A target the
      embeddings consent refuses, or whose slot cannot be added, is NOT filled
      (loud line) — the run keeps the recorded model.
    """
    if force_recreate:
        res = resolve_code_model(codegraph_prefix=codegraph_prefix, role="write",
                                 record_reader=lambda _p: None,
                                 project_target=target_from_binding(read_binding_row(codegraph_prefix)))
        slot, dim = code_model_slot(res.model)
        rec = CodeModelRecord(res.model, dim, slot)
        return AnalyzerRunPlan(record=rec, model=res.model, target=res.target,
                               force_recreate=True, refusal=str(res.refusal or ""))
    res = resolve_code_model(codegraph_prefix=codegraph_prefix, role="write")
    if res.recorded is None:
        live = record_from_live(codegraph_prefix, res.target, weaviate_url=weaviate_url)
        if live is not None:
            write_code_model_record(codegraph_prefix, live)
            res = resolve_code_model(codegraph_prefix=codegraph_prefix, role="write", record=live,
                                     project_target=target_from_binding(read_binding_row(codegraph_prefix)))
    if res.target_refused:
        print(f"[vct] {consent_refusal_for(res.target, res.target_source, _app_state_reader())} "
              f"{codegraph_prefix} keeps {res.recorded.model if res.recorded else res.model}.",
              file=sys.stderr)
    if res.refusal is not None:
        extra = (" The graph keeps its recorded OpenAI vectors; no fill to "
                 f"{res.target} starts until the consent is on (or rebuild with "
                 "--force-recreate)." if res.record_refused and not same_code_model(
                     res.target, res.model) else "")
        return AnalyzerRunPlan(record=res.recorded, model=res.model, refusal=str(res.refusal) + extra)
    if res.recorded is None:  # first build: make the target's slot exist, embed it
        err = ensure_slot_on_collections(codegraph_prefix, res.model, add=True,
                                         weaviate_url=weaviate_url)
        if err:
            return AnalyzerRunPlan(record=None, model=res.model, refusal=err)
        return AnalyzerRunPlan(record=None, model=res.model, target=res.target)
    base = res.recorded.model
    err = ensure_slot_on_collections(codegraph_prefix, base, add=False, weaviate_url=weaviate_url)
    if err:
        return AnalyzerRunPlan(record=res.recorded, model=base, refusal=(
            f"{err} Select the model this graph is indexed with, or rebuild it "
            "with --force-recreate."))
    if not (res.filling and res.model == res.target):
        return AnalyzerRunPlan(record=res.recorded, model=res.model, target=res.target)
    err = ensure_slot_on_collections(codegraph_prefix, res.target, add=True, weaviate_url=weaviate_url)
    if err:
        print(f"[vct] code-embed: cannot fill {res.target} yet ({err}); "
              f"{codegraph_prefix} keeps {base} this run.", file=sys.stderr)
        return AnalyzerRunPlan(record=res.recorded, model=base, role="query", target=res.target)
    print(_fill_line(codegraph_prefix, res, weaviate_url), flush=True)
    if not scoped:  # the whole-graph walk counts its own reads against the target slot
        from vco_lib import codegraph_guards

        codegraph_guards.start_fill_tally(code_model_slot(res.target)[0])
    return AnalyzerRunPlan(record=res.recorded, model=res.target, target=res.target,
                           filling=True, rewalk=not scoped)


def finish_analyzer_run(
    codegraph_prefix: str, plan: AnalyzerRunPlan, service: Any, *, scoped: bool,
    client_factory: Optional[Callable[[], Any]] = None,
    repo_root: Any = None, index_dot_claude: bool = False,
    stats: Optional[Mapping[str, Any]] = None, revision: Optional[int] = None,
) -> Optional[str]:
    """After a clean walk: a recreate stamps its model; a whole-graph run
    measures the target's slot and flips the record once it is complete (or
    stores the progress); a scoped run that met a pending fill hands it to the
    detached full walk (:func:`spawn_fill_walk`)."""
    if scoped and plan.filling and repo_root is not None:
        return spawn_fill_walk(repo_root, codegraph_prefix, index_dot_claude=index_dot_claude)
    from vco_lib import codegraph_guards

    tally = codegraph_guards.stop_fill_tally()
    if plan.force_recreate:
        model = str(getattr(service, "code_model_id", "") or plan.model)
        slot, dim = code_model_slot(model)
        write_code_model_record(codegraph_prefix, CodeModelRecord(
            model, int(getattr(service, "code_dim", 0) or dim), slot))
        return "complete"
    if scoped or plan.refusal or not (plan.filling or plan.record is None):
        return None
    target = plan.target or plan.model
    stats = stats or {}
    clean = not any(int(stats.get(k, 0) or 0) for k in
                    ("files_skipped", "insert_errors", "chunk_plan_failures"))
    counters = None
    if tally and clean and tally["slot"] == code_model_slot(target)[0]:
        # Every row the walk visited holds the target vector now: it embedded
        # the ``lacking`` ones and wrote nothing that failed. Those are exactly
        # the rows it is responsible for — NOT adjusted to the live count
        # (orphans would block the flip forever; review F1).
        counters = {"target": target, "filled": tally["visited"],
                    "total": tally["visited"], "revision": revision, "source": "walk"}
        print(f"[vct] code-embed: {codegraph_prefix}: the fill walk embedded "
              f"{tally['lacking']} missing {target} vector(s) across "
              f"{tally['visited']} row(s).", flush=True)
    return settle_fill(codegraph_prefix, target, counters=counters, client_factory=client_factory)


def fill_owed(codegraph_prefix: str) -> bool:
    """Is a fill owed — a selected target that differs from the record, with
    consent? The resync driver's spawn gate asks this (bundle update)."""
    try:
        res = resolve_code_model(codegraph_prefix=codegraph_prefix, role="write")
    except Exception:  # noqa: BLE001
        return False
    return res.filling and res.model == res.target


def spawn_fill_walk(repo_root: Any, codegraph_prefix: str, *, index_dot_claude: bool) -> Optional[str]:
    """A scoped (single-file / incremental) run cannot fill a whole slot; it
    hands the fill to the ordinary detached full walk (the resync driver, one
    claim per project). Soft-fail; returns the trigger status."""
    try:
        from pathlib import Path

        from vco_lib.codegraph_resync import spawn_background_resync

        result = spawn_background_resync(Path(repo_root), codegraph_prefix, check_owed=True,
                                         index_dot_claude=index_dot_claude)
        return getattr(result, "status", None)
    except Exception as exc:  # noqa: BLE001 — a scoped run never fails on this
        print(f"[vct] code-embed: could not hand the fill to a full walk ({exc})", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Status (Preferences) + the bundle update's self-heal
# ---------------------------------------------------------------------------


def project_status(
    folder: str, codegraph_prefix: str, *, count: bool = True,
    resolution: Optional[CodeModelResolution] = None, fill: Optional[dict] = None,
) -> "dict[str, Any]":
    res = resolution or resolve_code_model(codegraph_prefix=codegraph_prefix)
    progress = fill if (fill and fill.get("target") and same_code_model(
        str(fill["target"]), res.target)) else None
    objects = None
    if res.filling and progress is None and count:
        objects = count_code_objects(codegraph_prefix)
    return {
        "prefix": canonical_prefix(codegraph_prefix),
        "recorded_model": res.recorded.model if res.recorded else None,
        "target_model": res.target,
        "target_source": res.target_source,
        "filling": res.filling,
        "filled": progress.get("filled") if progress else None,
        "total": progress.get("total") if progress else objects,
        # W6-B2 final round (review nit): a graph whose RECORDED OpenAI model
        # the consent refuses is not complete — its searches are blocked.
        "complete": (bool(res.recorded) and not res.filling and not res.target_refused
                     and not res.record_refused),
        "cost_bearing": is_openai_code_model(res.target),
        "target_refused": res.target_refused,
        "record_refused": res.record_refused,
        "folder": folder,
    }


def status_all(*, count: bool = True) -> "dict[str, Any]":
    from vco_lib.launcher_db_reader import list_codegraph_binding_rows
    from vco_lib.openai_consent import embeddings_consent_granted

    target, target_source = resolve_code_model_pick()
    projects = []
    for row in list_codegraph_binding_rows() or []:
        prefix = str(row.get("collection_prefix") or "")
        if not prefix:
            continue
        res = resolve_code_model(codegraph_prefix=prefix, record=record_from_binding(row),
                                 record_reader=lambda _p: None,
                                 project_target=target_from_binding(row))
        entry = project_status(str(row.get("folder_path") or ""), prefix, count=count,
                               resolution=res, fill=fill_from_binding(row))
        entry.update(project_id=row.get("project_id"), name=row.get("name"))
        projects.append(entry)
    return {"target": target, "target_source": target_source,
            "target_cost_bearing": is_openai_code_model(target),
            "openai_embeddings_consent": embeddings_consent_granted(),
            "projects": projects}


def reconcile_code_embedding(folder: Any, codegraph_prefix: str) -> Optional[str]:
    """The bundle update's self-heal for one graph (no embedding here): stamp
    a missing record off live data, then — while a target fills — measure its
    slot: complete → flip (the instant switch back to a still-complete slot),
    else store the progress. The embedding itself happens in the next analyzer
    run (launcher build, incremental sync, the resync driver). Soft-fail."""
    try:
        res = resolve_code_model(codegraph_prefix=codegraph_prefix)
        if res.recorded is None:
            live = record_from_live(codegraph_prefix, res.target)
            if live is not None and write_code_model_record(codegraph_prefix, live):
                res = resolve_code_model(codegraph_prefix=codegraph_prefix, record=live,
                                         project_target=target_from_binding(read_binding_row(codegraph_prefix)))
        if res.filling:
            return settle_fill(codegraph_prefix, res.target,
                               counters=fill_from_binding(read_binding_row(codegraph_prefix)))
        return None
    except Exception as exc:  # noqa: BLE001 — never aborts a bundle run
        print(f"[vct] code-embed: self-heal skipped for {folder} ({exc})", file=sys.stderr)
        return None


def reconcile_folder_code_embedding(folder: Any, *, dry_run: bool = False) -> Optional[str]:
    """The bundle-run entry point (every ``install-bundle`` and the root bundle
    inside install.py): :func:`reconcile_code_embedding` for the folder's
    REGISTERED graph."""
    if dry_run:
        return None
    try:
        from vco_lib.project_identity import resolve_identity

        identity, _snap = resolve_identity(folder)
    except Exception:  # noqa: BLE001 — an unreadable registry is "not known"
        return None
    if identity is None or not identity.registered or not identity.codegraph_prefix:
        return None
    return reconcile_code_embedding(folder, identity.codegraph_prefix)


def main(argv: Optional["list[str]"] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m vco_lib.code_embed_model")
    sub = parser.add_subparsers(dest="cmd", required=True)
    st = sub.add_parser("status", help="JSON: per-project recorded vs target code model + fill")
    st.add_argument("--json", action="store_true", help="No-op: output is always JSON.")
    st.add_argument("--no-count", action="store_true", help="Skip the Weaviate object counts.")
    args = parser.parse_args(argv)
    if args.cmd == "status":
        json.dump(status_all(count=not args.no_count), sys.stdout, default=str)
        sys.stdout.write("\n")
        return 0
    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
