# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Plain-HTTP Weaviate read adapter for the hook KG producer (v0.2.102 W6-M).

The hook KG producer used to import ``weaviate_mcp.server`` — weaviate-client
plus the MCP SDK, ~0.7 s of every prompt — to run three read calls. This module
serves the SAME read calls over Weaviate's GraphQL endpoint with the standard
library only, behind the subset of the weaviate-client v4 surface the moved KG
query layer (``kg_query_core``) and the RL enrichment layer call:

  ``client.collections.get(name).query.near_vector(...)`` /
  ``.near_text(...)`` / ``.fetch_objects(...)`` and
  ``Filter.by_property(p).<op>(v)`` combined with ``&`` / ``|``.

Result objects carry what v4 returns for those calls: ``.uuid`` (a
``uuid.UUID``), ``.properties`` (EVERY non-reference property, ``None`` for an
unset one, typed as v4 types them — ``date`` → aware ``datetime``, ``uuid`` →
``uuid.UUID``, ``number`` → ``float``, nested ``object``/``object[]`` → dicts),
``.metadata`` (``distance`` / ``certainty`` / ``score`` / ``creation_time`` /
``last_update_time``; ``None`` unless requested) and ``.vector`` (``{}`` unless
``include_vector=True``, then ``{named_vector: [...]}`` — ``{"default": [...]}``
for an unnamed legacy vector).

Filter values are typed from the collection SCHEMA, the way the server coerces
a v4 gRPC filter: ``Filter.by_property("chunk_num").equal(2)`` on a ``number``
property is sent as ``valueNumber`` (GraphQL refuses ``valueInt`` there, gRPC
accepts it). Unknown properties fall back to the Python type of the value.

A keyword this adapter does not implement raises ``TypeError`` — never a
silent ignore that would change what a query returns. Reads only: there is no
write method, by design.
"""

from __future__ import annotations

import json
import urllib.error
from array import array
import urllib.request
import uuid as _uuid
from datetime import datetime, timezone
from typing import Any, Iterable, Optional


class WeaviateUnreachable(Exception):
    """Raised when Weaviate is not reachable (HTTP refused / gRPC down).

    Carries a user-actionable message in `.user_msg` for the MCP layer to
    surface to the agent. The bare exception message stays terse; the
    full hint (commands to verify + recover) is in user_msg.
    """
    def __init__(self, msg: str, user_msg: str):
        super().__init__(msg)
        self.user_msg = user_msg


class WeaviateHttpQueryError(Exception):
    """A GraphQL/REST read failed (HTTP error or GraphQL ``errors``)."""


#: Init / query timeouts — the weaviate-client v4 defaults (``Timeout(init=2,
#: query=30)``), so a slow Weaviate is waited on exactly as long as before.
INIT_TIMEOUT_S = 2.0
QUERY_TIMEOUT_S = 30.0

_SCALAR_TYPES = {
    "text", "text[]", "string", "string[]", "int", "int[]", "number", "number[]",
    "boolean", "boolean[]", "date", "date[]", "uuid", "uuid[]",
}
_VALUE_KEY_BY_TYPE = {
    "text": "valueText", "string": "valueText", "uuid": "valueText",
    "int": "valueInt", "number": "valueNumber", "boolean": "valueBoolean",
    "date": "valueDate",
}
_METADATA_FIELDS = {
    "distance": "distance",
    "certainty": "certainty",
    "score": "score",
    "creation_time": "creationTimeUnix",
    "last_update_time": "lastUpdateTimeUnix",
}


# ─── Filters ─────────────────────────────────────────────────────────────


def _gql_value(value: Any) -> str:
    if isinstance(value, datetime):
        return json.dumps(value.isoformat())
    if isinstance(value, _uuid.UUID):
        return json.dumps(str(value))
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_gql_value(v) for v in value) + "]"
    return json.dumps(value)


def _value_key(value: Any, data_type: str) -> str:
    base = data_type[:-2] if data_type.endswith("[]") else data_type
    if base in _VALUE_KEY_BY_TYPE:
        return _VALUE_KEY_BY_TYPE[base]
    probe = value[0] if isinstance(value, (list, tuple)) and value else value
    if isinstance(probe, bool):
        return "valueBoolean"
    if isinstance(probe, int):
        return "valueInt"
    if isinstance(probe, float):
        return "valueNumber"
    if isinstance(probe, datetime):
        return "valueDate"
    return "valueText"


class _Where:
    """One where-clause node; ``&`` / ``|`` build And / Or trees."""

    def __and__(self, other: "_Where") -> "_Where":
        return _Combined("And", [self, other])

    def __or__(self, other: "_Where") -> "_Where":
        return _Combined("Or", [self, other])

    def to_graphql(self, prop_types: "dict[str, str]") -> str:  # pragma: no cover - abstract
        raise NotImplementedError


class _Leaf(_Where):
    def __init__(self, prop: str, operator: str, value: Any) -> None:
        self.prop = prop
        self.operator = operator
        self.value = value

    def to_graphql(self, prop_types: "dict[str, str]") -> str:
        if self.operator == "IsNull":
            return (f"{{operator: IsNull, path: [{json.dumps(self.prop)}], "
                    f"valueBoolean: {json.dumps(bool(self.value))}}}")
        key = _value_key(self.value, prop_types.get(self.prop, ""))
        return (f"{{operator: {self.operator}, path: [{json.dumps(self.prop)}], "
                f"{key}: {_gql_value(self.value)}}}")


class _Combined(_Where):
    def __init__(self, operator: str, operands: "list[_Where]") -> None:
        self.operator = operator
        self.operands = operands

    def to_graphql(self, prop_types: "dict[str, str]") -> str:
        inner = ", ".join(o.to_graphql(prop_types) for o in self.operands)
        return f"{{operator: {self.operator}, operands: [{inner}]}}"


class _ByProperty:
    def __init__(self, name: str) -> None:
        self._name = name

    def _leaf(self, operator: str, value: Any) -> _Leaf:
        return _Leaf(self._name, operator, value)

    def equal(self, value: Any) -> _Leaf:
        return self._leaf("Equal", value)

    def not_equal(self, value: Any) -> _Leaf:
        return self._leaf("NotEqual", value)

    def less_than(self, value: Any) -> _Leaf:
        return self._leaf("LessThan", value)

    def less_or_equal(self, value: Any) -> _Leaf:
        return self._leaf("LessThanEqual", value)

    def greater_than(self, value: Any) -> _Leaf:
        return self._leaf("GreaterThan", value)

    def greater_or_equal(self, value: Any) -> _Leaf:
        return self._leaf("GreaterThanEqual", value)

    def like(self, value: str) -> _Leaf:
        return self._leaf("Like", value)

    def contains_any(self, values: "Iterable[Any]") -> _Leaf:
        return self._leaf("ContainsAny", list(values))

    def contains_all(self, values: "Iterable[Any]") -> _Leaf:
        return self._leaf("ContainsAll", list(values))

    def is_none(self, value: bool) -> _Leaf:
        return self._leaf("IsNull", value)


class Filter:
    """The ``weaviate.classes.query.Filter`` subset the KG read path uses."""

    @staticmethod
    def by_property(name: str) -> _ByProperty:
        return _ByProperty(name)

    @staticmethod
    def all_of(filters: "list[_Where]") -> _Where:
        return filters[0] if len(filters) == 1 else _Combined("And", list(filters))

    @staticmethod
    def any_of(filters: "list[_Where]") -> _Where:
        return filters[0] if len(filters) == 1 else _Combined("Or", list(filters))


# ─── Results ─────────────────────────────────────────────────────────────


class MetadataReturn:
    """v4 ``MetadataReturn`` shape: every field present, ``None`` unless asked."""

    __slots__ = ("creation_time", "last_update_time", "distance", "certainty",
                 "score", "explain_score", "is_consistent", "rerank_score")

    def __init__(self, **values: Any) -> None:
        for name in self.__slots__:
            setattr(self, name, values.get(name))

    def __repr__(self) -> str:
        inner = ", ".join(f"{n}={getattr(self, n)!r}" for n in self.__slots__)
        return f"MetadataReturn({inner})"


class HttpObject:
    __slots__ = ("uuid", "properties", "metadata", "vector", "collection", "references")

    def __init__(self, uuid, properties, metadata, vector, collection) -> None:
        self.uuid = uuid
        self.properties = properties
        self.metadata = metadata
        self.vector = vector
        self.collection = collection
        self.references = None


class QueryReturn:
    __slots__ = ("objects",)

    def __init__(self, objects: "list[HttpObject]") -> None:
        self.objects = objects


def _parse_date(raw: Any) -> Any:
    if not isinstance(raw, str):
        return raw
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _parse_uuid(raw: Any) -> Any:
    if not isinstance(raw, str):
        return raw
    try:
        return _uuid.UUID(raw)
    except ValueError:
        return raw


def _convert(raw: Any, data_type: str, nested: "list[dict] | None") -> Any:
    """Type one GraphQL property value the way weaviate-client v4 does."""
    if raw is None:
        return None
    is_array = data_type.endswith("[]")
    base = data_type[:-2] if is_array else data_type
    if is_array and isinstance(raw, list):
        return [_convert(v, base, nested) for v in raw]
    if base == "date":
        return _parse_date(raw)
    if base == "uuid":
        return _parse_uuid(raw)
    if base == "number" and isinstance(raw, int) and not isinstance(raw, bool):
        return float(raw)
    if base == "object" and isinstance(raw, dict):
        types = {p["name"]: (p.get("dataType") or [""])[0] for p in (nested or [])}
        subs = {p["name"]: p.get("nestedProperties") for p in (nested or [])}
        return {k: _convert(v, types.get(k, ""), subs.get(k)) for k, v in raw.items()}
    return raw


# ─── Collections ─────────────────────────────────────────────────────────


def _selection(props: "list[dict]") -> str:
    parts = []
    for p in props:
        dtype = (p.get("dataType") or [""])[0]
        base = dtype[:-2] if dtype.endswith("[]") else dtype
        if dtype in _SCALAR_TYPES:
            parts.append(p["name"])
        elif base == "object" and p.get("nestedProperties"):
            parts.append(f"{p['name']} {{ {_selection(p['nestedProperties'])} }}")
        # cross-references (class-name dataTypes) are not returned by a v4
        # query unless asked for, so they are not selected here either.
    return " ".join(parts)


class _Query:
    def __init__(self, coll: "HttpCollection") -> None:
        self._coll = coll

    def near_vector(self, near_vector, *, limit=None, offset=None, filters=None,
                    return_metadata=None, include_vector=False, target_vector=None,
                    **unsupported) -> QueryReturn:
        _refuse(unsupported, "near_vector")
        vec = ", ".join(repr(float(x)) for x in near_vector)
        arg = f"nearVector: {{vector: [{vec}]"
        if target_vector:
            arg += f", targetVectors: [{json.dumps(target_vector)}]"
        arg += "}"
        return self._coll._get(arg, limit=limit, offset=offset, filters=filters,
                               return_metadata=return_metadata, include_vector=include_vector)

    def near_text(self, query, *, limit=None, offset=None, filters=None,
                  return_metadata=None, include_vector=False, target_vector=None,
                  **unsupported) -> QueryReturn:
        _refuse(unsupported, "near_text")
        concepts = [query] if isinstance(query, str) else list(query)
        arg = f"nearText: {{concepts: {_gql_value(concepts)}"
        if target_vector:
            arg += f", targetVectors: [{json.dumps(target_vector)}]"
        arg += "}"
        return self._coll._get(arg, limit=limit, offset=offset, filters=filters,
                               return_metadata=return_metadata, include_vector=include_vector)

    def fetch_objects(self, *, limit=None, offset=None, filters=None,
                      return_metadata=None, include_vector=False,
                      **unsupported) -> QueryReturn:
        _refuse(unsupported, "fetch_objects")
        return self._coll._get("", limit=limit, offset=offset, filters=filters,
                               return_metadata=return_metadata, include_vector=include_vector)


def _refuse(unsupported: dict, method: str) -> None:
    if unsupported:
        raise TypeError(
            f"kg_http.{method}: unsupported argument(s) {sorted(unsupported)} — "
            "the plain-HTTP read adapter implements only what the KG read path uses"
        )


def _metadata_names(return_metadata: Any) -> "list[str]":
    if not return_metadata:
        return []
    if isinstance(return_metadata, str):
        names = [return_metadata]
    else:
        names = list(return_metadata)
    bad = [n for n in names if n not in _METADATA_FIELDS]
    if bad:
        raise TypeError(f"kg_http: unsupported return_metadata {bad}")
    return names


class HttpCollection:
    def __init__(self, client: "HttpWeaviateClient", name: str) -> None:
        self._client = client
        self.name = name
        self.query = _Query(self)
        self._schema: "Optional[dict]" = None

    def _class_schema(self) -> dict:
        if self._schema is None:
            self._schema = self._client._get_json(f"/v1/schema/{self.name}")
        return self._schema

    def _get(self, search_arg: str, *, limit, offset, filters, return_metadata,
             include_vector) -> QueryReturn:
        schema = self._class_schema()
        props = [p for p in schema.get("properties") or [] if p.get("name")]
        prop_types = {p["name"]: (p.get("dataType") or [""])[0] for p in props}
        nested = {p["name"]: p.get("nestedProperties") for p in props}
        args = [a for a in (search_arg,) if a]
        if filters is not None:
            args.append(f"where: {filters.to_graphql(prop_types)}")
        if limit is not None:
            args.append(f"limit: {int(limit)}")
        if offset:
            args.append(f"offset: {int(offset)}")
        meta = _metadata_names(return_metadata)
        additional = ["id"] + [_METADATA_FIELDS[m] for m in meta]
        vector_names = sorted((schema.get("vectorConfig") or {}).keys())
        if include_vector:
            additional.append(f"vectors {{ {' '.join(vector_names)} }}" if vector_names else "vector")
        arg_text = f"({', '.join(args)})" if args else ""
        query = (f"{{ Get {{ {self.name}{arg_text} {{ {_selection(props)} "
                 f"_additional {{ {' '.join(additional)} }} }} }} }}")
        payload = self._client._post_json("/v1/graphql", {"query": query})
        if payload.get("errors"):
            raise WeaviateHttpQueryError(
                "; ".join(str(e.get("message", e)) for e in payload["errors"]))
        rows = ((payload.get("data") or {}).get("Get") or {}).get(self.name) or []
        objects = []
        for row in rows:
            extra = row.get("_additional") or {}
            properties = {
                p["name"]: _convert(row.get(p["name"]), prop_types[p["name"]], nested.get(p["name"]))
                for p in props if p["name"] in row
            }
            metadata = MetadataReturn(**{
                m: (_unix_ms(extra.get(_METADATA_FIELDS[m]))
                    if m in ("creation_time", "last_update_time") else _f32(extra.get(_METADATA_FIELDS[m])))
                for m in meta
            })
            if include_vector:
                vector = ({k: _f32_vector(v) for k, v in (extra.get("vectors") or {}).items()}
                          if vector_names
                          else ({"default": _f32_vector(extra["vector"])} if extra.get("vector") else {}))
            else:
                vector = {}
            objects.append(HttpObject(_parse_uuid(extra.get("id")), properties, metadata,
                                      vector, self.name))
        return QueryReturn(objects)


def _f32(raw: Any) -> Any:
    """A float the way v4's gRPC transport delivers it: float32-exact.

    gRPC carries distances and vectors as float32; GraphQL serialises the same
    float32 as its shortest decimal, which parses to a DIFFERENT float64
    (0.03096459 vs 0.030964590…). Rounding through float32 recovers the exact
    value v4 returns, so scores, ``emb``/``n_emb`` and every cosine computed
    from them are bit-identical across the two transports.
    """
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return array("f", [raw])[0]
    return raw


def _f32_vector(raw: Any) -> Any:
    if isinstance(raw, list) and all(isinstance(x, (int, float)) for x in raw):
        return array("f", raw).tolist()
    return raw


def _unix_ms(raw: Any) -> Any:
    try:
        return datetime.fromtimestamp(int(raw) / 1000, tz=timezone.utc)
    except (TypeError, ValueError):
        return raw


class _Collections:
    def __init__(self, client: "HttpWeaviateClient") -> None:
        self._client = client
        self._cache: "dict[str, HttpCollection]" = {}

    def get(self, name: str) -> HttpCollection:
        if name not in self._cache:
            self._cache[name] = HttpCollection(self._client, name)
        return self._cache[name]


class HttpWeaviateClient:
    """Read-only Weaviate client over plain HTTP (GraphQL + schema REST).

    Construction probes ``/v1/.well-known/ready`` (as the v4 connect does) so an
    unreachable Weaviate fails HERE, loudly, rather than as an empty result.
    """

    def __init__(self, base_url: str, *, init_timeout: float = INIT_TIMEOUT_S,
                 query_timeout: float = QUERY_TIMEOUT_S) -> None:
        self.base_url = base_url.rstrip("/")
        self._query_timeout = query_timeout
        self.collections = _Collections(self)
        try:
            with urllib.request.urlopen(f"{self.base_url}/v1/.well-known/ready",
                                        timeout=init_timeout) as resp:
                status = resp.status
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise WeaviateUnreachable(
                f"Connection to Weaviate failed. Details: {exc}",
                f"Is Weaviate running and reachable at {self.base_url}?",
            ) from exc
        if status != 200:
            raise WeaviateUnreachable(
                f"Weaviate at {self.base_url} is not ready (HTTP {status})",
                f"Weaviate at {self.base_url} answered but is not ready yet; retry shortly.",
            )

    def _request(self, path: str, body: "Optional[dict]" = None) -> dict:
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}{path}", data=data,
            headers={"Content-Type": "application/json"} if data is not None else {},
            method="POST" if data is not None else "GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._query_timeout) as resp:
                return json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            raise WeaviateHttpQueryError(f"HTTP {exc.code} on {path}") from exc

    def _get_json(self, path: str) -> dict:
        return self._request(path)

    def _post_json(self, path: str, body: dict) -> dict:
        return self._request(path, body)

    def close(self) -> None:
        """Nothing to release: urllib holds no connection between calls."""
