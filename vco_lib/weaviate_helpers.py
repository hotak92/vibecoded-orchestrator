# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Reusable helpers for safe Weaviate GraphQL interaction.

Centralizes the "inspect errors[] array BEFORE consuming data" pattern
that v0.2.42-v0.2.45's recurring re-embed bug was rooted in. See
knowledge/concepts/silent-zero-fallback-antipattern.md (instance #3) +
knowledge/concepts/mcp-loud-fail-error-pattern.md (GraphQL errors[]
sub-pattern).

By GraphQL convention, errors can be present even on HTTP 200 OK
responses. The bug pattern is:

    body = json.loads(resp.read())
    objs = body.get("data", {}).get("Get", {}).get(coll, []) or []
    # ^ silently coalesces (200 OK + errors[]) -> [] -> "0 objects found"

Callers should instead use ``post_graphql_safe`` (one-call wrapper) or
``check_graphql_errors`` (just the errors gate) so they inherit the
protection by default.

v0.2.92 W18 — this module is also the HOME OF RECORD for the tri-state
:class:`ProbeResult` (``present`` / ``absent`` / ``unknown``) and for the ONE
class-existence probe :func:`probe_class_listing`. The rule it exists to
enforce: *a check that cannot distinguish "I could not determine this" from
"this is fine" is not a check.* Three shipped helpers used to answer "which
classes exist?" by returning ``[]`` on a transport failure, so an unreachable
Weaviate and an empty Weaviate were the same answer — and one consumer of that
answer widened a destructive recommendation because of it.

It lives HERE, rather than in a module of its own, because
``weaviate_helpers`` is ``vco_lib``'s dependency-free leaf (stdlib imports
only), so every other ``vco_lib`` module — including the sqlite-only
``config_projection`` — can import it with no risk of an import cycle. The
merge lane may hoist :class:`ProbeResult` next to ``vco_lib.doctor``'s
``STATUS_*`` constants; the two vocabularies already share the word that
matters (``unknown``) and ``tests/test_v0292_wp2_class_probe_ssot.py`` pins
them equal so they cannot drift apart before that happens.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Callable, Generic, Optional, TypeVar

# Default Weaviate HTTP port. Single source of truth for the "no explicit
# WEAVIATE_URL" fallback across every Python caller (project_init,
# weaviate_schema, install.py all mirror this constant — converged here in
# v0.2.77 Part 7a so the fallback can never drift).
DEFAULT_WEAVIATE_PORT = 8081

# Default gRPC port for the weaviate-client v4 `connect_to_custom` calls.
DEFAULT_GRPC_PORT = 50052


# ═══════════════════════════════════════════════════════════════════════════
# v0.2.92 W18 — the tri-state probe result
# ═══════════════════════════════════════════════════════════════════════════
#
# Three states, never two:
#
#   present  the probe RAN and the thing is there (``_value`` carries it)
#   absent   the probe RAN and the thing is genuinely NOT there
#   unknown  the probe COULD NOT RUN — this is not evidence of anything
#
# ``unknown`` is the whole point. Before this type, "Weaviate is down" and
# "Weaviate has no such class" produced the identical value (``[]`` / ``None``
# / ``False``), so a guard that consumed it could not tell a missing thing
# from a missing answer.
#
# The state strings deliberately reuse ``vco_lib.doctor``'s vocabulary for the
# one word that carries the meaning: ``PROBE_UNKNOWN == doctor.STATUS_UNKNOWN``
# ("unknown"). The doctor answers a HEALTH question (ok/problem/unknown); this
# answers an EXISTENCE question (present/absent/unknown). Same third state,
# same rule, different domain — pinned equal by test so a future consolidation
# has nothing to reconcile.

#: The probe RAN; the thing exists.
PROBE_PRESENT = "present"
#: The probe RAN; the thing genuinely does not exist.
PROBE_ABSENT = "absent"
#: The probe COULD NOT RUN. NOT "absent". Same string as
#: ``vco_lib.doctor.STATUS_UNKNOWN`` by design.
PROBE_UNKNOWN = "unknown"

#: Every legal :attr:`ProbeResult.state` value.
PROBE_STATES = (PROBE_PRESENT, PROBE_ABSENT, PROBE_UNKNOWN)

T = TypeVar("T")


class ProbeUnavailable(RuntimeError):
    """A caller tried to consume a probe that could not run.

    Raised by :meth:`ProbeResult.require`. Carries ``what`` (the question the
    probe was asking) and ``reason`` (why it could not be answered) so the
    message a user eventually sees names both.
    """

    def __init__(self, what: str, reason: str) -> None:
        self.what = what
        self.reason = reason
        super().__init__(
            f"could not determine {what}: {reason} "
            "(this is NOT evidence of absence — refuse to act on it)"
        )


class ProbeResult(Generic[T]):
    """Tri-state answer to an existence question.

    Construct through :meth:`present` / :meth:`absent` / :meth:`unknown`;
    consume through :meth:`require` (loud) or :meth:`or_none` (the
    ``Optional`` view). ``.state`` is one of :data:`PROBE_STATES`.

    **The value is deliberately not a public attribute** and the three
    accidental collapses are deliberately fatal:

        if listing: ...        -> TypeError  (was: unknown reads as truthy)
        if not listing: ...    -> TypeError  (was: unknown reads as empty)
        for c in listing: ...  -> TypeError  (was: unknown iterates as empty)
        len(listing)           -> TypeError  (was: unknown measures as 0)
        listing or []          -> TypeError

    A caller that has not decided what "could not determine" means for its own
    decision therefore fails loudly at the first use, instead of quietly
    reading the unknown state as "fine". That is the mechanism; the docstring
    is only the explanation.
    """

    __slots__ = ("state", "what", "_value", "_reason")

    def __init__(
        self,
        state: str,
        what: str,
        value: Optional[T] = None,
        reason: str = "",
    ) -> None:
        if state not in PROBE_STATES:
            raise ValueError(
                f"ProbeResult state must be one of {PROBE_STATES!r}, "
                f"got {state!r}"
            )
        self.state = state
        self.what = what
        self._value = value
        self._reason = reason

    # ── construction ────────────────────────────────────────────────────

    @classmethod
    def present(cls, value: T, *, what: str) -> "ProbeResult[T]":
        """The probe ran and found ``value``."""
        return cls(PROBE_PRESENT, what, value=value)

    @classmethod
    def absent(
        cls, *, what: str, value: Optional[T] = None
    ) -> "ProbeResult[T]":
        """The probe ran and the thing is genuinely not there.

        ``value`` is the canonical "nothing" for the domain — ``[]`` for a
        listing (so :meth:`require` stays iterable), ``None`` for a scalar.
        """
        return cls(PROBE_ABSENT, what, value=value)

    @classmethod
    def unknown(cls, reason: str, *, what: str) -> "ProbeResult[T]":
        """The probe could NOT run. ``reason`` says why, for the user."""
        return cls(PROBE_UNKNOWN, what, reason=reason)

    # ── queries ─────────────────────────────────────────────────────────

    def is_present(self) -> bool:
        return self.state == PROBE_PRESENT

    def is_absent(self) -> bool:
        """True only when the probe RAN and proved the thing is not there."""
        return self.state == PROBE_ABSENT

    def is_unknown(self) -> bool:
        """True when the probe could not run. Never conflate with absence."""
        return self.state == PROBE_UNKNOWN

    def is_known(self) -> bool:
        """True when the probe ran at all (present OR absent)."""
        return self.state != PROBE_UNKNOWN

    @property
    def reason(self) -> str:
        """Why the probe could not run. ``""`` when it ran."""
        return self._reason

    # ── consumption ─────────────────────────────────────────────────────

    def require(self) -> T:
        """Return the value, or raise :class:`ProbeUnavailable` if unknown.

        The default consumption path: a caller that has no answer for "could
        not determine" gets an exception rather than a wrong answer.
        """
        if self.state == PROBE_UNKNOWN:
            raise ProbeUnavailable(self.what, self._reason)
        return self._value  # type: ignore[return-value]

    def or_none(self) -> Optional[T]:
        """The ``Optional`` view: ``None`` means COULD NOT CHECK.

        This is the ``Optional[list]`` convention the guard lane arrived at
        independently (``None`` = query failed, ``[]`` = read and empty) —
        reuse it rather than inventing another.

        Only correct where the ABSENT value is itself not ``None`` (a listing,
        whose absent value is ``[]``). For a SCALAR probe whose absent value
        is ``None`` this re-creates the very conflation the type removes, so
        branch on :meth:`is_unknown` there instead.
        """
        if self.state == PROBE_UNKNOWN:
            return None
        return self._value

    # ── the collapse guards (the mechanism) ─────────────────────────────

    def __bool__(self) -> bool:
        raise TypeError(
            f"ProbeResult({self.what!r}) is tri-state and has no truth value: "
            "`if probe:` would read 'could not determine' as 'fine'. "
            "Use .is_present() / .is_absent() / .is_unknown()."
        )

    def __iter__(self) -> Any:
        raise TypeError(
            f"ProbeResult({self.what!r}) is not iterable: iterating would read "
            "'could not determine' as 'empty'. Use .require() (raises when "
            "unknown) or .or_none()."
        )

    def __len__(self) -> int:
        raise TypeError(
            f"ProbeResult({self.what!r}) has no length: len() would read "
            "'could not determine' as 0. Use .require() or .or_none()."
        )

    # ── plumbing ────────────────────────────────────────────────────────

    def __repr__(self) -> str:
        if self.state == PROBE_UNKNOWN:
            return f"ProbeResult(unknown, what={self.what!r}, reason={self._reason!r})"
        return (
            f"ProbeResult({self.state}, what={self.what!r}, "
            f"value={self._value!r})"
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ProbeResult):
            return NotImplemented
        return (
            self.state == other.state
            and self.what == other.what
            and self._value == other._value
            and self._reason == other._reason
        )

    __hash__ = None  # type: ignore[assignment]  # mutable-by-convention


#: The question :func:`probe_class_listing` answers, used in every message.
WHAT_CLASS_LISTING = "which classes exist in Weaviate"


def probe_class_listing(
    weaviate_url: Optional[str] = None,
    *,
    timeout: float = 10.0,
    request: Optional[Callable[..., tuple[int, bytes]]] = None,
) -> "ProbeResult[list[str]]":
    """ONE ``GET /v1/schema`` -> a tri-state listing of every class name.

    This is the single home for "which classes exist on this server?". Every
    other class-existence question in ``vco_lib`` routes through it, so no
    caller can re-invent the ``[]``-on-transport-failure shape.

    Returns:
        * ``present`` — the schema was read and names at least one class;
          :meth:`~ProbeResult.require` gives the names in server order.
        * ``absent`` — the schema was read and holds no classes;
          :meth:`~ProbeResult.require` gives ``[]``.
        * ``unknown`` — the schema could NOT be read: transport failure,
          non-200, unparsable body, or a payload with no ``classes`` array.
          **Never** rendered as an empty listing.

    A payload without a usable ``classes`` array is deliberately ``unknown``
    rather than ``absent``: a live Weaviate always answers ``{"classes": [...]}``
    (empty array on a fresh server), so a missing array means we did not
    understand the response — and "conservative" here means refusing, not
    reporting an empty server. Same choice
    ``project_init.probe_classes_exist`` already makes.

    ``request`` is a test seam with the :func:`http_request` signature; the
    default is resolved at call time so ``mock.patch`` on this module's
    ``http_request`` also works. Never raises.
    """
    base = (weaviate_url or weaviate_url_default()).rstrip("/")
    url = f"{base}/v1/schema"
    req = request or http_request
    try:
        status, body = req("GET", url, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 — could not check != absent
        return ProbeResult.unknown(
            f"GET {url} failed: {type(exc).__name__}: {exc}",
            what=WHAT_CLASS_LISTING,
        )
    if status != 200:
        return ProbeResult.unknown(
            f"GET {url} -> HTTP {status}", what=WHAT_CLASS_LISTING,
        )
    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        return ProbeResult.unknown(
            f"GET {url} -> unparsable response: {type(exc).__name__}: {exc}",
            what=WHAT_CLASS_LISTING,
        )
    classes = payload.get("classes") if isinstance(payload, dict) else None
    if not isinstance(classes, list):
        return ProbeResult.unknown(
            f"GET {url} -> response carries no 'classes' array",
            what=WHAT_CLASS_LISTING,
        )
    names = [
        str(c.get("class"))
        for c in classes
        if isinstance(c, dict)
        and isinstance(c.get("class"), str)
        and c.get("class")
    ]
    if not names:
        return ProbeResult.absent(what=WHAT_CLASS_LISTING, value=[])
    return ProbeResult.present(names, what=WHAT_CLASS_LISTING)


def weaviate_url_default() -> str:
    """Return the WEAVIATE_URL env value, or the canonical localhost default.

    Read from the environment on each call (never cached at import) so tests
    and hooks that mutate ``os.environ["WEAVIATE_URL"]`` between calls see the
    live value.
    """
    return os.environ.get(
        "WEAVIATE_URL", f"http://localhost:{DEFAULT_WEAVIATE_PORT}"
    )


def http_request(
    method: str,
    url: str,
    *,
    body: Optional[dict] = None,
    timeout: float = 30.0,
) -> tuple[int, bytes]:
    """Thin urllib wrapper. Returns ``(status, body_bytes)``. Never raises on
    non-2xx — the caller decides what to do.

    On an :class:`urllib.error.HTTPError` the error body is drained and
    returned alongside the HTTP code so the caller can inspect Weaviate's
    reason. Transport errors (connection refused, DNS, timeout) still raise —
    callers that want a soft-fail wrap the call in try/except themselves
    (matching the historical project_init / weaviate_schema contract).
    """
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return (resp.status, resp.read())
    except urllib.error.HTTPError as e:
        # Drain the error body so the caller can inspect Weaviate's reason.
        try:
            return (e.code, e.read())
        except Exception:
            return (e.code, b"")


def fetch_schema(
    name: str, weaviate_url: Optional[str] = None
) -> Optional[dict]:
    """GET /v1/schema/<name>. Returns the schema dict on 200, None on 404,
    raises :class:`RuntimeError` on any other status.

    Transport errors (server unreachable) propagate from :func:`http_request`
    — the caller catches them when it wants a soft-fail.
    """
    base = (weaviate_url or weaviate_url_default()).rstrip("/")
    status, body = http_request("GET", f"{base}/v1/schema/{name}")
    if status == 200:
        return json.loads(body.decode("utf-8"))
    if status == 404:
        return None
    raise RuntimeError(
        f"GET /v1/schema/{name} → HTTP {status}: {body[:200]!r}"
    )


# v0.2.92 W18: ``list_classes`` (``[]`` on any transport failure) was REMOVED,
# not re-homed. It was the fourth copy of the class-listing question and the
# only one with no caller at all — a loaded gun kept for a user who never
# arrived. :func:`probe_class_listing` above answers the same question without
# the conflation; ``.require()`` reproduces the old shape for anyone who wants
# a plain list, minus the silent lie.


def connect_v4(
    weaviate_url: Optional[str] = None,
    *,
    grpc_port: Optional[int] = None,
    grpc_secure: bool = False,
    http_secure: Optional[bool] = None,
    skip_init_checks: bool = True,
):
    """Late-import weaviate-client v4 and return a connected client.

    Single factory for the ``weaviate.connect_to_custom`` pattern that used
    to be hand-rolled in project_init, sync_knowledge_graph, and the MCP
    server with slightly divergent parameters. Converged here in v0.2.77
    Part 7a. The weaviate dependency is imported lazily so non-Weaviate code
    paths don't pull it.

    Args:
        weaviate_url: e.g. ``http://localhost:8081``. Defaults to
            :func:`weaviate_url_default`.
        grpc_port: gRPC port. Defaults to ``$GRPC_PORT`` env or
            :data:`DEFAULT_GRPC_PORT` (50052). Callers with a different
            historical default MUST pass it explicitly.
        grpc_secure: TLS for the gRPC channel. Default False.
        http_secure: TLS for the HTTP channel. When None (default), derived
            from whether ``weaviate_url`` starts with ``https://``.
        skip_init_checks: pass-through to weaviate-client. Default True
            (matches the migrate/bootstrap contract — avoids a startup probe
            round-trip on every connect).
    """
    import weaviate  # noqa: WPS433  (intentional lazy import)

    url = weaviate_url or weaviate_url_default()
    host = url.replace("http://", "").replace("https://", "").split(":")[0]
    # Defensive port parse (works for "http://localhost:8081" or
    # "https://host:9999/").
    try:
        port = int(url.rsplit(":", 1)[-1].split("/")[0])
    except ValueError:
        port = DEFAULT_WEAVIATE_PORT
    if grpc_port is None:
        grpc_port = int(os.environ.get("GRPC_PORT", str(DEFAULT_GRPC_PORT)))
    if http_secure is None:
        http_secure = url.startswith("https://")
    return weaviate.connect_to_custom(
        http_host=host,
        http_port=port,
        http_secure=http_secure,
        grpc_host=host,
        grpc_port=grpc_port,
        grpc_secure=grpc_secure,
        skip_init_checks=skip_init_checks,
    )


def count_objects_v4(name: str, weaviate_url: Optional[str] = None) -> int:
    """Count objects in a collection via the v4 iterator (metadata-only, no
    vectors). Returns ``0`` on connection failure OR missing collection.

    SEMANTICS — 0-on-failure: this variant collapses "unreachable" and
    "empty" into 0. Use it ONLY where a conservative "treat as empty" is the
    safe decision (e.g. the migrate crash-recovery classifier, where a
    connection failure means "can't classify, don't act destructively" and 0
    routes to the leave-alone branch). When you need to DISTINGUISH
    unreachable from genuinely-empty, use :func:`http_count_objects` instead
    (it returns None on failure). See the count-semantics note in
    ``knowledge/concepts/silent-zero-fallback-antipattern.md``.
    """
    try:
        client = connect_v4(weaviate_url=weaviate_url)
    except Exception:
        # Connection failure → can't classify; treat as 0 (caller decides the
        # conservative action from there).
        return 0
    try:
        col = client.collections.get(name)
        n = 0
        for _ in col.iterator(include_vector=False):
            n += 1
        return n
    except Exception:
        # Collection might not exist yet, or the v4 client raises on a missing
        # class; missing → 0.
        return 0
    finally:
        client.close()


def http_count_objects(
    class_name: str, weaviate_url: Optional[str] = None
) -> Optional[int]:
    """Count objects in ``class_name`` via the Weaviate GraphQL Aggregate
    endpoint.

    SEMANTICS — None-on-failure (the counterpart to
    :func:`count_objects_v4`'s 0-on-failure):

        int   — object count when the request succeeds (0 for a genuinely
                empty / missing class per Aggregate's empty-list contract).
        None  — Weaviate unreachable, non-200, or malformed response. The
                caller treats None as "unknown" (NOT zero) so it never claims
                a collection is empty when the server was simply unreachable.

    Soft-fails throughout: never raises into the caller.
    """
    base = (weaviate_url or weaviate_url_default()).rstrip("/")
    query = "{ Aggregate { " f"{class_name} {{ meta {{ count }} }}" " } }"
    try:
        status, body = http_request(
            "POST", f"{base}/v1/graphql", body={"query": query}, timeout=10.0,
        )
    except Exception:
        return None
    if status != 200:
        return None
    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception:
        return None
    try:
        agg = payload.get("data", {}).get("Aggregate", {}) or {}
        rows = agg.get(class_name) or []
        if not rows:
            # Aggregate returns an empty list for a missing class.
            return 0
        meta = rows[0].get("meta") or {}
        count = meta.get("count")
        if isinstance(count, int):
            return count
    except Exception:
        return None
    return None


class WeaviateGraphQLError(Exception):
    """Raised when a Weaviate GraphQL response carries a non-empty errors array.

    Carries the full errors list for caller inspection. HTTP status is
    irrelevant — by GraphQL convention errors can be present even on 200 OK.
    """

    def __init__(self, errors: list[dict[str, Any]], ctx: str = ""):
        self.errors = errors
        self.ctx = ctx
        first = errors[0].get("message", "unknown") if errors else "unknown"
        super().__init__(f"{ctx}: {first[:200]}")


def check_graphql_errors(
    body: dict[str, Any],
    ctx: str = "graphql",
    on_error: Optional[Callable[[list[dict[str, Any]]], None]] = None,
) -> bool:
    """Returns True if errors array present (and calls on_error if given).
    Returns False if errors absent (caller should consume data normally).

    Pattern:
        body = json.loads(resp.read())
        if check_graphql_errors(body, ctx="ci10_diff_gate"):
            return {}  # or whatever the failure-path return is
        data = body["data"]  # safe to consume now

    on_error callback receives the errors list; useful for logging via
    the caller's _log_install_event or similar. Exceptions raised by the
    callback are swallowed so logging failure can never block the caller.
    """
    errors = body.get("errors")
    if not errors:
        return False
    if on_error:
        try:
            on_error(errors)
        except Exception:
            pass  # logging failure should never block the caller
    return True


def post_graphql_safe(
    weaviate_url: str,
    gql: dict[str, Any],
    ctx: str = "graphql",
    timeout: float = 30.0,
    on_error: Optional[Callable[[list[dict[str, Any]]], None]] = None,
) -> Optional[dict[str, Any]]:
    """POST a GraphQL query to Weaviate; return the ``data`` field on success,
    None on errors-array OR HTTP failure (caller decides recovery).

    ``on_error`` is called with the errors list when present. ``on_error`` is
    also called with a synthetic error-list when the HTTP transport fails
    (e.g., ``[{"message": "transport: <details>"}]``) so the caller has a
    single observability path.

    Example caller:
        def _batch_query_weaviate_content_hashes(coll, url):
            gql = {"query": f"{{ Get {{ {coll}(limit: 10000) "
                            f"{{ file_path content_hash }} }} }}"}
            data = post_graphql_safe(url, gql, ctx=f"hash-query-{coll}",
                                     on_error=lambda errs: log_warn(coll, errs))
            if data is None:
                return {}
            objs = data.get("Get", {}).get(coll) or []
            return {o["file_path"]: o.get("content_hash", "") for o in objs if o.get("file_path")}
    """
    try:
        req = urllib.request.Request(
            f"{weaviate_url}/v1/graphql",
            data=json.dumps(gql).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read())
    except Exception as exc:
        if on_error:
            try:
                on_error([{"message": f"transport: {exc}"}])
            except Exception:
                pass
        return None

    if check_graphql_errors(body, ctx=ctx, on_error=on_error):
        return None

    return body.get("data")
