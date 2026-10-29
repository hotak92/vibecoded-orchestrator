# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Vector-index policy for VCO's Weaviate collections (v0.2.105): RQ-8
compression and a per-collection ``vectorCacheMaxObjects``.

The values live in ONE rule table, ``vco_lib/weaviate_vector_index.toml``,
which the launcher's Rust KG-collection create embeds too (tier B of A>B>C).
This module is the Python side:

* **At creation** — :func:`create_index_config` (REST shape, for the class
  definitions in :mod:`vco_lib.project_init`) and :func:`client_named_vector`
  (the ``weaviate`` client shape, for the template scripts) set the cache cap.
  RQ-8 for a NEW collection comes from the server default
  ``DEFAULT_QUANTIZATION: "rq-8"`` in VCO's compose, which reaches every create
  path at once (Python REST, Python client, the launcher's Rust create) and
  never sends an RQ field to an older, adopted Weaviate that would reject it.
* **On every install/update** — :func:`apply_policy` brings EXISTING
  collections to the policy: it enables RQ-8 on an uncompressed HNSW vector
  (Weaviate >= 1.32; verified on 1.37.18 to compress a populated index in
  place, no re-embed, no data copy) and lowers an UNLIMITED cache to the cap.
  Idempotent (a conformant collection is not touched), soft-failing per
  collection, and it never drops, copies or re-creates anything.

Leave-alone rules (each tested both ways): only VCO-shaped classes
(:data:`vco_lib.service_detection.WEAVIATE_EXACT_MARKERS` /
``WEAVIATE_SUFFIX_MARKERS``); only ``hnsw`` vectors; a vector that already has
any compression (PQ / SQ / BQ / RQ, the user's or ours) keeps it; a cache cap
someone already set keeps its value; a server older than the table's
``min_server_version`` gets no RQ.
"""
from __future__ import annotations

import copy
import functools
import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

CONFIG_PATH = Path(__file__).resolve().parent / "weaviate_vector_index.toml"
#: Table format this loader understands. MUST MATCH
#: ``VECTOR_INDEX_TABLE_FORMAT_VERSION`` in
#: ``launcher/src-tauri/src/commands/project_state_cmd.rs``.
FORMAT_VERSION = 1

#: Weaviate's own ``vectorCacheMaxObjects`` default (1e12 = unlimited). A
#: value at or above it is "never set"; anything lower is someone's choice.
WEAVIATE_UNLIMITED_CACHE = 1_000_000_000_000

#: The deferral a failed update step records (declared in
#: ``vco_lib/deferral_conditions.toml``).
CID_UPDATE_FAILED = "weaviate_vector_index_update_failed"

KIND_CODE = "code"
KIND_KG = "kg"


@dataclass(frozen=True)
class Policy:
    rq_bits: int
    rq_rescore_limit: int
    rq_min_server_version: tuple[int, int, int]
    cache_max_objects: Mapping[str, int]


@functools.lru_cache(maxsize=1)
def policy() -> Policy:
    """The parsed rule table. Raises on a malformed table (it ships with the
    code; a defect is a test failure, never a user's)."""
    from vco_lib.version_compare import parse_version  # noqa: PLC0415

    data = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    if data.get("format_version") != FORMAT_VERSION:
        raise ValueError(f"{CONFIG_PATH.name}: format_version must be {FORMAT_VERSION}")
    rq = data["rq"]
    cache = data["vector_cache_max_objects"]
    return Policy(
        rq_bits=int(rq["bits"]),
        rq_rescore_limit=int(rq["rescore_limit"]),
        rq_min_server_version=parse_version(str(rq["min_server_version"])),
        cache_max_objects={KIND_CODE: int(cache[KIND_CODE]), KIND_KG: int(cache[KIND_KG])},
    )


def collection_kind(class_name: str) -> Optional[str]:
    """``"code"`` / ``"kg"`` for a class VCO creates, ``None`` for any other
    (a user's own class on a shared Weaviate is never touched). Identity is
    the ONE marker set service detection uses to recognise VCO data."""
    from vco_lib.service_detection import (  # noqa: PLC0415
        WEAVIATE_EXACT_MARKERS,
        WEAVIATE_SUFFIX_MARKERS,
    )
    from vco_lib.weaviate_schema import is_code_collection  # noqa: PLC0415

    name = class_name or ""
    lowered = name.lower()
    is_vco = name in WEAVIATE_EXACT_MARKERS or any(
        lowered.endswith(s.lower()) for s in WEAVIATE_SUFFIX_MARKERS)
    if not is_vco:
        return None
    return KIND_CODE if is_code_collection(name) else KIND_KG


def cache_max_objects(class_name: str) -> int:
    """The cache cap for *class_name* (a non-VCO name gets the text cap)."""
    return policy().cache_max_objects[collection_kind(class_name) or KIND_KG]


def create_index_config(class_name: str) -> dict:
    """``vectorIndexConfig`` for an HNSW named vector at class creation (REST
    shape). Only the cache cap: RQ comes from the server default (see the
    module docstring), so the same body still creates on a pre-RQ Weaviate."""
    return {"vectorCacheMaxObjects": cache_max_objects(class_name)}


def client_named_vector(slot_name: str, class_name: str) -> Any:
    """``Configure.NamedVectors.none(...)`` with the cache cap — the client
    shape of :func:`create_index_config`, for the template scripts that
    create collections through the ``weaviate`` client."""
    from weaviate.classes.config import Configure  # noqa: PLC0415

    return Configure.NamedVectors.none(
        name=slot_name,
        vector_index_config=Configure.VectorIndex.hnsw(
            vector_cache_max_objects=cache_max_objects(class_name)),
    )


def server_supports_rq(version: Optional[str]) -> bool:
    """True when *version* (``/v1/meta``'s ``version``) is at or above the
    table's ``min_server_version``. An unreadable version is ``False``."""
    from vco_lib.version_compare import VersionParseError, parse_version  # noqa: PLC0415

    try:
        return parse_version(str(version or "").split("-", 1)[0]) >= policy().rq_min_server_version
    except VersionParseError:
        return False


_COMPRESSIONS = ("pq", "sq", "bq", "rq")


def _has_compression(index_cfg: Mapping[str, Any]) -> bool:
    return any(bool((index_cfg.get(k) or {}).get("enabled")) for k in _COMPRESSIONS)


def plan_class_update(class_schema: Mapping[str, Any], *, rq_supported: bool) -> Optional[dict]:
    """Pure: the class body to ``PUT /v1/schema/<class>`` so *class_schema*
    (as ``GET /v1/schema/<class>`` returns it) meets the policy, or ``None``
    when nothing is owed (or the class is not VCO's).

    Per HNSW vector (each named vector, or the legacy class-level index):
    RQ-8 when ``rq_supported`` and the vector has no compression yet; the
    cache cap when the live value is Weaviate's unlimited default.
    """
    name = str(class_schema.get("class") or "")
    kind = collection_kind(name)
    if kind is None:
        return None
    pol = policy()
    cap = pol.cache_max_objects[kind]
    body = copy.deepcopy(dict(class_schema))
    changed = False

    def _fix(holder: dict) -> bool:
        if str(holder.get("vectorIndexType") or "hnsw").lower() != "hnsw":
            return False
        cfg = holder.get("vectorIndexConfig")
        if not isinstance(cfg, dict):
            return False
        touched = False
        if rq_supported and not _has_compression(cfg):
            cfg["rq"] = {**(cfg.get("rq") or {}), "enabled": True,
                         "bits": pol.rq_bits, "rescoreLimit": pol.rq_rescore_limit}
            touched = True
        live_cap = cfg.get("vectorCacheMaxObjects")
        if live_cap is None or int(live_cap) >= WEAVIATE_UNLIMITED_CACHE:
            if live_cap != cap:
                cfg["vectorCacheMaxObjects"] = cap
                touched = True
        return touched

    vector_config = body.get("vectorConfig")
    if isinstance(vector_config, dict) and vector_config:
        for slot in vector_config.values():
            if isinstance(slot, dict) and _fix(slot):
                changed = True
    elif _fix(body):
        changed = True
    return body if changed else None


@dataclass
class ApplyResult:
    """What :func:`apply_policy` did. ``unreachable`` = the server could not
    be read at all (nothing was attempted)."""
    server_version: Optional[str] = None
    rq_supported: bool = False
    updated: list[str] = field(default_factory=list)
    conformant: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    unreachable: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.unreachable is None and not self.failed


RequestFn = Callable[..., tuple[int, bytes]]


def apply_policy(weaviate_url: Optional[str] = None, *,
                 request: Optional[RequestFn] = None,
                 timeout: float = 600.0) -> ApplyResult:
    """Bring every existing VCO collection on *weaviate_url* to the policy
    (see :func:`plan_class_update`). Reads ``/v1/meta`` and ``/v1/schema``
    once, then one ``PUT /v1/schema/<class>`` per class that owes a change.
    Never raises; a per-class failure is collected and the rest go on. The
    long per-PUT timeout is deliberate: enabling RQ compresses the index on
    the server, and a slow machine must not be cut off mid-way."""
    from vco_lib import weaviate_helpers as _wh  # noqa: PLC0415

    req = request or _wh.http_request
    base = (weaviate_url or _wh.weaviate_url_default()).rstrip("/")
    res = ApplyResult()
    try:
        st, raw = req("GET", f"{base}/v1/meta", timeout=15.0)
        if st != 200:
            res.unreachable = f"GET /v1/meta answered HTTP {st}"
            return res
        res.server_version = str(json.loads(raw.decode("utf-8")).get("version") or "")
        st, raw = req("GET", f"{base}/v1/schema", timeout=60.0)
        if st != 200:
            res.unreachable = f"GET /v1/schema answered HTTP {st}"
            return res
        classes = json.loads(raw.decode("utf-8")).get("classes") or []
    except Exception as exc:  # noqa: BLE001 — transport/parse → a soft failure
        res.unreachable = f"{type(exc).__name__}: {exc}"
        return res
    res.rq_supported = server_supports_rq(res.server_version)
    for cls in classes:
        name = str((cls or {}).get("class") or "")
        if collection_kind(name) is None:
            continue
        body = plan_class_update(cls, rq_supported=res.rq_supported)
        if body is None:
            res.conformant.append(name)
            continue
        try:
            # Load the class's shards first (one object read). Weaviate loads
            # shards lazily, and a PUT that turns RQ on for a shard that is NOT
            # loaded only stores the setting: the index then stays
            # uncompressed until its next write (verified on 1.37.18). With the
            # shard loaded, the same PUT compresses it in place right away.
            req("GET", f"{base}/v1/objects?class={name}&limit=1", timeout=timeout)
            st, raw = req("PUT", f"{base}/v1/schema/{name}", body=body, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 — one class, not the run
            res.failed.append((name, f"{type(exc).__name__}: {exc}"))
            continue
        if st == 200:
            res.updated.append(name)
        else:
            res.failed.append((name, f"HTTP {st}: {raw[:300].decode('utf-8', 'replace')}"))
    return res


def run_update_step(deferral_report, *, weaviate_url: Optional[str] = None,
                    log_event: Optional[Callable[..., None]] = None,
                    request: Optional[RequestFn] = None) -> ApplyResult:
    """install.py's step: :func:`apply_policy`, a printed summary, an install
    log event (the record), and on failure the
    ``weaviate_vector_index_update_failed`` deferral — owned by install.py,
    so the next run that succeeds drops it."""
    from vco_lib.deferral_report import DeferralEntry  # noqa: PLC0415

    res = apply_policy(weaviate_url, request=request)
    data = {"server_version": res.server_version, "rq_supported": res.rq_supported,
            "updated": res.updated, "conformant": len(res.conformant),
            "failed": [n for n, _ in res.failed], "unreachable": res.unreachable}
    if log_event is not None:
        try:
            log_event("7e/10", "ok" if res.ok else "warn", "weaviate vector-index policy", data=data)
        except Exception:  # noqa: BLE001 — logging never breaks the step
            pass
    print("[7e/10] Weaviate vector index (RQ-8 compression, cache cap) ... ", end="")
    if res.unreachable is not None:
        print(f"skipped ({res.unreachable})")
    else:
        note = "" if res.rq_supported else (
            f"; Weaviate {res.server_version or '?'} has no RQ, cache cap only")
        print(f"{len(res.updated)} updated, {len(res.conformant)} already set, "
              f"{len(res.failed)} failed{note}")
        for name, why in res.failed:
            print(f"  [vector-index] {name}: {why}")
    if res.ok:
        return res
    if res.unreachable is not None:
        detected = (f"The step that compresses VCO's Weaviate collections (RQ-8) and caps "
                    f"their vector cache could not read the server: {res.unreachable}.")
    else:
        lines = "\n".join(f"  - {n}: {w}" for n, w in res.failed)
        detected = ("Weaviate refused the vector-index update for these collections:\n"
                    f"{lines}")
    deferral_report.add_entry(DeferralEntry(
        condition_id=CID_UPDATE_FAILED,
        title="Weaviate collections not yet compressed / cache not capped",
        detected=detected,
        why_deferred=("Nothing was dropped or copied: the collections keep working "
                      "uncompressed, they only use more memory. Every install/update "
                      "retries the step; it only changes index settings."),
        command_to_apply="python install.py --update",
        severity="warning",
        kg_node_refs=[],
    ))
    return res
