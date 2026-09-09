# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Path, port and knob resolution for the gateway.

Every path resolves through :mod:`vco_lib.paths` — the state root is never
rebuilt inline from the user's home directory, there is no hardcoded
orchestrator root, and no venv is assumed. That matters because the gateway
runs on machines whose state root is redirected by ``VCT_STATE_DIR`` (dev
launchers) and whose home differs from the build host's.

(The forbidden inline form is deliberately not spelled out above: the
repo-wide gate ``tests/test_vct_root_dir_consolidation.py`` scans raw file
text and does not exempt docstrings, so naming the pattern in prose reads as
committing it.)

``vco_lib`` is imported at module scope on purpose. It is part of every healthy
install (``install.py`` runs ``pip install -e .`` for the root distribution
before installing this one), so a failing import means a BROKEN install and
must surface loudly rather than degrade to an inline copy of the resolver.

Environment knobs (all optional; every one is read by code in this package)
--------------------------------------------------------------------------
``VCT_MODEL_GATEWAY_PORT``
    TCP port, and the ONLY way to pin one: it is what ``--port`` sets, and an
    explicitly requested port is never moved (see :data:`FALLBACK_PORT_RANGE`).
    Unset, the port resolves through :func:`resolve_port`:
    ``<vct_root>/model-gateway.port`` (the running daemon's file, the same
    convention ``hub.port`` uses), then ``<vct_root>/model-gateway.last-port``
    (the record of the port a gateway was last STARTED on, which outlives the
    daemon), then :data:`DEFAULT_PORT`.
``VCT_MODEL_GATEWAY_HOST``
    Bind address. Defaults to ``127.0.0.1`` and is REFUSED unless it resolves
    to a loopback address — see :func:`resolve_host`.
``VCT_MODEL_GATEWAY_CREDENTIALS``
    Path to the Claude CLI's credentials file. Defaults to
    ``<claude_user_dir()>/.credentials.json``.
``VCT_MODEL_GATEWAY_CONTEXT_TABLE``
    Path to the exported chat-model context table. Defaults to
    ``<vct_root>/model-gateway/chat_model_context.json``.
``VCT_MODEL_GATEWAY_SECRET_PROJECT``
    Project scope passed to the vct-secrets resolver. Default: unset, which
    means the shared scope in the file store and a by-path lookup in the hub.
    Set it when the vendor key was stored against a specific project.
``VCT_MODEL_GATEWAY_CATALOG_TTL`` / ``VCT_MODEL_GATEWAY_STATIC_RETRY_TTL`` /
``VCT_MODEL_GATEWAY_KEY_TTL``
    Cache lifetimes in seconds. Present so the smoke tests can drive the
    cache without sleeping; documented because a knob nobody can find is a
    knob that gets re-invented.
``VCT_MODEL_GATEWAY_REWRITE_BUFFER_BYTES``
    How much of a request body the daemon will HOLD in order to rewrite ids
    in it, in bytes. Defaults to :data:`REWRITE_BUFFER_LIMIT_BYTES`. It is
    not a request ceiling — a body past it is forwarded upstream as a stream,
    unrewritten, never refused — so lowering it trades the rewrite for
    memory, and raising it does the reverse. A non-numeric or non-positive
    value falls back to the default (:func:`_env_int`): an unbounded buffer
    is not a state a typo should be able to reach.
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from vco_lib.intfile import read_int_line
from vco_lib.paths import claude_user_dir, vct_root_dir

#: Documented in CLAUDE.md as the model-router port. The field prototype ran
#: on 8787; the shipped daemon uses the documented port and the collision with
#: a hand-run prototype is resolved by retiring the prototype.
DEFAULT_PORT = 11436

DEFAULT_HOST = "127.0.0.1"

#: What ``/health`` puts in its ``service`` field, and therefore the ONE
#: string that identifies an answering process as this daemon. It lives here
#: rather than beside the handler because two callers need it and they must
#: not be able to drift: the handler that WRITES it
#: (:func:`model_router.server.health_handler`) and the startup probe that
#: READS it (:func:`model_router.__main__._probe_running_gateway`), which
#: decides whether a foreign listener on the gateway's port is this daemon
#: already running or somebody else's service.
SERVICE_NAME = "vct-model-gateway"

#: Live catalogs are re-fetched at most this often.
DEFAULT_CATALOG_TTL_S = 6 * 3600

#: When a live fetch FAILS and the static fallback is serving, retry this much
#: sooner. The prototype cached the fallback for the full 6 h, so a vendor
#: outage of one minute cost six hours of a stale picker.
DEFAULT_STATIC_RETRY_TTL_S = 300

#: Vendor keys are re-resolved at most this often, so a rotation is picked up
#: without restarting and a hub that was down at boot is retried.
DEFAULT_KEY_TTL_S = 300

#: Default seconds of upstream SILENCE tolerated on a relayed response. See
#: :attr:`GatewayConfig.upstream_idle_timeout_s` — this is an idle bound, and
#: there is deliberately no total one.
DEFAULT_UPSTREAM_IDLE_TIMEOUT_S = 1800.0

#: Seconds to wait for the TCP connection to an upstream. Short and separate
#: from the idle bound: failing to CONNECT is a different condition from a
#: slow answer, and it is the one that should fail fast — a wrong or dead
#: upstream must surface as a 502 the client can retry, not as a half-hour
#: hang. Not a knob: no machine needs a different value, and the one thing
#: that could go wrong (a network that takes 40 s to connect) is better
#: reported than waited on.
UPSTREAM_CONNECT_TIMEOUT_S = 30

#: How much of a request body the gateway will HOLD in memory so it can
#: rewrite ids inside it. **Not a request ceiling**: nothing is refused for
#: being bigger — a body past this bound is forwarded upstream as a stream,
#: unrewritten, and the upstream's own answer is relayed (see the body-size
#: policy in :mod:`model_router.server`).
#:
#: A bound has to exist because the gateway is not a pass-through: it rewrites
#: the model id, and a vendor's tool ids, inside the body, which means holding
#: it. 32 MiB is chosen to sit just ABOVE Anthropic's documented 32 MB Messages
#: API request ceiling — the limit behind the 413 ``request_too_large`` in
#: https://docs.claude.com/en/api/errors, and the number Claude Code quotes
#: back at the user ("Request too large (max 32MB)"). Rounding UP to MiB is the
#: safe direction: every body the first-party upstream can accept is one the
#: gateway rewrites, and only bodies that upstream would refuse anyway reach
#: the streaming path.
#:
#: The 2026-09-09 field defect is why the distinction is spelled out. aiohttp's
#: DEFAULT ``client_max_size`` is 1 MiB, and with the body buffered that
#: default made the gateway answer 413 to every ``POST /v1/messages`` of a
#: conversation past roughly 250K tokens of context — far less with images —
#: while the same conversation worked natively against Anthropic. The client
#: renders any 413 as an accumulated-attachments problem, so the gateway's own
#: refusal read as the user's fault. A buffer bound must therefore never be
#: reachable as a refusal.
#:
#: The same NUMBER as :data:`model_router.tool_ids.SSE_BUFFER_LIMIT_BYTES` and
#: deliberately not shared with it: that one bounds an unframed RESPONSE
#: stream and is ours to re-tune, this one is pinned to a PUBLISHED request
#: limit and moves only when Anthropic's documentation does. Same value today,
#: different reasons to change, so they are two constants rather than one with
#: two meanings.
REWRITE_BUFFER_LIMIT_BYTES = 32 * 1024 * 1024

_TOKEN_BASENAME = "model-gateway.token"
_PID_BASENAME = "model-gateway.pid"
_PORT_BASENAME = "model-gateway.port"

#: Record of the port a gateway was last STARTED on. Written on every
#: successful bind and NEVER deleted — unlike the port file, which is the
#: running daemon's own and answers "where is it now?". This one answers
#: "where was it last?", and that is the question a resolver has after the
#: daemon has exited. MUST MATCH ``LAST_PORT_BASENAME`` in
#: ``launcher/src-tauri/src/commands/model_gateway.rs`` and the constant of
#: the same name in ``vco_lib/vscode_settings.py`` — both are declared in the
#: switch lane (v0.2.94) and land at merge, so a tree in which only this
#: declaration exists is an expected intermediate state, not a drift.
LAST_PORT_BASENAME = "model-gateway.last-port"

#: Ports tried, in order, when the resolved port is taken and no port was
#: explicitly requested. MUST MATCH ``FALLBACK_PORT_RANGE`` in
#: ``launcher/src-tauri/src/commands/model_gateway.rs`` (``11460..=11468``);
#: the range is inclusive there and this ``range`` stops one past its end.
#:
#: The 2026-09-08 machine is why it exists: a legacy container owned the
#: documented default, so a login-started daemon died on a bind error every
#: time and the only visible symptom was a gateway that "does not start".
#: Nine ports is enough for any plausible number of local services and small
#: enough to stay a documented, predictable list rather than a scan.
#:
#: It sits at 11460 rather than just above the default because the ports
#: immediately after it are already SPOKEN FOR by this codebase:
#:
#: ===== ==================================================================
#: 11439 legacy RL server (``RL_SERVER_URL``, ``weaviate_mcp/server.py``)
#: 11440 code-embedding service (``vco_lib.code_embed_image.DEFAULT_PORT``)
#: 11442 orchestrator-root RL (``ORCHESTRATOR_ROOT_RL_PORT``, Rust)
#: 11443 global RL (``GLOBAL_RL_PORT``, Rust)
#: ===== ==================================================================
#:
#: A fallback that lands on a reserved port either loses the race to that
#: service or WINS it and takes the service's port away; both reproduce, one
#: layer along, exactly the collision this range exists to escape. 11460-11468
#: is clear of every port literal shipped in this repo and sits below the
#: per-project RL window (11500-11900).
FALLBACK_PORT_RANGE = range(11460, 11469)
_STATE_SUBDIR = "model-gateway"
_EXPORT_BASENAME = "chat_model_context.json"
_LOG_BASENAME = "model-gateway.log"


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def token_path() -> Path:
    return vct_root_dir() / _TOKEN_BASENAME


def pid_path() -> Path:
    return vct_root_dir() / _PID_BASENAME


def port_path() -> Path:
    return vct_root_dir() / _PORT_BASENAME


def export_path() -> Path:
    """Where this daemon LOOKS for an exported chat-model context table.

    The schema is documented in :mod:`model_router.context_table`. No writer
    of this file exists in the tree yet; absence is the normal state and the
    shipped seed covers it. This path and that schema are the requirement any
    future writer has to meet.
    """
    custom = (os.environ.get("VCT_MODEL_GATEWAY_CONTEXT_TABLE") or "").strip()
    if custom:
        return Path(custom)
    return vct_root_dir() / _STATE_SUBDIR / _EXPORT_BASENAME


def log_path() -> Path:
    return vct_root_dir() / "logs" / _LOG_BASENAME


def credentials_path() -> Path:
    """The file the Claude CLI writes and refreshes its OAuth token into.

    HARNESS-OWNED: this package only ever READS it, never writes it, and never
    copies its contents anywhere.
    """
    custom = (os.environ.get("VCT_MODEL_GATEWAY_CREDENTIALS") or "").strip()
    if custom:
        return Path(custom)
    return claude_user_dir() / ".credentials.json"


def last_port_path() -> Path:
    """Beside the port file, by design: same directory, different question."""
    return vct_root_dir() / LAST_PORT_BASENAME


def explicit_port() -> Optional[int]:
    """The port the USER asked for, or ``None``.

    ``--port`` writes ``VCT_MODEL_GATEWAY_PORT`` before anything reads it, so
    "explicit" is exactly "that variable holds a usable port". The distinction
    is load-bearing: an explicit port is a pin and is never moved, while a
    RESOLVED one is a best guess and may fall back.
    """
    raw = (os.environ.get("VCT_MODEL_GATEWAY_PORT") or "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if 1 <= value <= 65535 else None


def _read_port_file(path: Path) -> Optional[int]:
    """A port from ``path``, or ``None``. Never raises.

    One home for both files, and the READING itself is one home further out:
    :func:`vco_lib.intfile.read_int_line` is the single "first line of a
    small state file, as an int" reader that the pid, port and lock files all
    go through. Corrupt, empty, out-of-range and missing all answer the same
    way — ``None``, meaning "no evidence here" — because these are read on
    every status poll and a damaged one must fall through to the next source
    rather than blank the answer.
    """
    return read_int_line(path, minimum=1, maximum=65535)


def port_candidates() -> list[int]:
    """Every port there is EVIDENCE a gateway may be on, best first.

    The same chain :func:`resolve_port` walks, kept whole instead of reduced
    to its head, because two callers want different ends of it. A caller that
    must CHOOSE a port wants the best single guess. A caller that must FIND a
    daemon somebody else started wants all of them: the port file and the
    last-port record can name different ports — a start that fell back
    rewrites both, but one that died leaves a stale file beside a record
    naming the port before it — and the daemon is on exactly one.

    Deduplicated, order preserved, so the common case (every source agreeing)
    probes one port rather than four.
    """
    ordered = [
        explicit_port(),
        _read_port_file(port_path()),
        _read_port_file(last_port_path()),
        DEFAULT_PORT,
    ]
    unique: list[int] = []
    for value in ordered:
        if value is not None and value not in unique:
            unique.append(value)
    return unique


def resolve_port() -> int:
    """env -> port file -> last-port record -> :data:`DEFAULT_PORT`.

    The HEAD of :func:`port_candidates`, so the order cannot exist in two
    versions. Each step is EVIDENCE and the default is the answer only when
    there is none. The third step is what keeps a gateway that fell back off
    the documented port findable after it has stopped: the port file belongs
    to a RUNNING daemon, so on its own the chain forgets a stopped one and
    answers with a default that, on the machine this was written for, is
    somebody else's service. MUST MATCH ``resolve_port`` in
    ``launcher/src-tauri/src/commands/model_gateway.rs`` and
    ``vco_lib.vscode_settings.resolve_gateway_ports`` — both of those read
    the last-port record too; they are authored in the switch lane (v0.2.94)
    and land at merge, so a tree in which only this implementation walks the
    full chain is a staging state rather than a divergence.
    """
    return port_candidates()[0]


class HostNotLoopbackError(ValueError):
    """A non-loopback bind address was requested."""


def is_loopback(host: str) -> bool:
    """True when every address ``host`` resolves to is loopback.

    ``ipaddress`` alone is not enough: ``localhost`` is a name. Resolution
    failure is NOT loopback — an unknown name must not be bound.
    """
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError):
        return False
    if not infos:
        return False
    import ipaddress

    for info in infos:
        address = info[4][0]
        try:
            if not ipaddress.ip_address(address).is_loopback:
                return False
        except ValueError:
            return False
    return True


def resolve_host() -> str:
    """Bind address, refused unless loopback.

    The host token authorises proxying under the user's Claude login and
    vendor subscription. Binding it to a routable interface would expose both
    to the network, so a non-loopback value is an error at startup rather than
    a footgun that only shows up in someone else's traffic.
    """
    host = (os.environ.get("VCT_MODEL_GATEWAY_HOST") or "").strip() or DEFAULT_HOST
    if not is_loopback(host):
        raise HostNotLoopbackError(
            f"VCT_MODEL_GATEWAY_HOST={host!r} is not a loopback address. The "
            "gateway proxies under your Claude login and vendor subscription "
            "and is authorised by a local file token; it binds loopback only.",
        )
    return host


@dataclass
class GatewayConfig:
    """Everything the app factory needs. Built once, at startup."""

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    token: str = ""
    credentials_file: Path = field(default_factory=credentials_path)
    context_table_file: Path = field(default_factory=export_path)
    seed_context_table: Path | None = None
    static_catalog_file: Path | None = None
    catalog_ttl_s: int = DEFAULT_CATALOG_TTL_S
    static_retry_ttl_s: int = DEFAULT_STATIC_RETRY_TTL_S
    key_ttl_s: int = DEFAULT_KEY_TTL_S
    secret_project: str | None = None
    #: Seconds of SILENCE from the upstream before the relay gives up — an
    #: IDLE timeout, never a total one. The distinction is the 2026-09-09
    #: 04:18 incident: a ``total`` of 600 s cut a stream that was still
    #: delivering, at 601 s, and the client saw the gateway end a working
    #: answer that native would have finished. A total budget cannot be
    #: correct here — a long completion legitimately runs for as long as it
    #: runs — so the only sound question is "has the upstream gone quiet?".
    #:
    #: 1800 s is deliberately above the client's own idle clamp, so when both
    #: are waiting the CLIENT is the one that gives up first and reports it in
    #: its own words. It is never a first-byte limit in practice either: the
    #: client's own first-byte window for a 30 MB body is ~599 s.
    #:
    #: Float, not int: the chaos suite drives sub-second idleness, and a knob
    #: that cannot express 0.2 s cannot be tested without sleeping for
    #: minutes.
    upstream_idle_timeout_s: float = DEFAULT_UPSTREAM_IDLE_TIMEOUT_S
    #: Seconds to wait for a catalog fetch. Short: ``/v1/models`` is the
    #: picker's blocking call, and a stalled vendor must fall back to the
    #: static catalog rather than hang the picker open.
    catalog_timeout_s: int = 10
    #: How much of a request body is held for the id rewrite, in bytes. Read
    #: by :func:`model_router.server.messages_handler`; a body past it is
    #: streamed upstream unrewritten rather than refused. See
    #: :data:`REWRITE_BUFFER_LIMIT_BYTES`.
    rewrite_buffer_bytes: int = REWRITE_BUFFER_LIMIT_BYTES

    @classmethod
    def from_env(cls, *, token: str = "") -> "GatewayConfig":
        return cls(
            host=resolve_host(),
            port=resolve_port(),
            token=token,
            credentials_file=credentials_path(),
            context_table_file=export_path(),
            catalog_ttl_s=_env_int(
                "VCT_MODEL_GATEWAY_CATALOG_TTL", DEFAULT_CATALOG_TTL_S,
            ),
            static_retry_ttl_s=_env_int(
                "VCT_MODEL_GATEWAY_STATIC_RETRY_TTL", DEFAULT_STATIC_RETRY_TTL_S,
            ),
            key_ttl_s=_env_int("VCT_MODEL_GATEWAY_KEY_TTL", DEFAULT_KEY_TTL_S),
            rewrite_buffer_bytes=_env_int(
                "VCT_MODEL_GATEWAY_REWRITE_BUFFER_BYTES",
                REWRITE_BUFFER_LIMIT_BYTES,
            ),
            secret_project=(
                (os.environ.get("VCT_MODEL_GATEWAY_SECRET_PROJECT") or "").strip()
                or None
            ),
        )


__all__ = [
    "DEFAULT_CATALOG_TTL_S",
    "DEFAULT_HOST",
    "DEFAULT_KEY_TTL_S",
    "DEFAULT_PORT",
    "DEFAULT_STATIC_RETRY_TTL_S",
    "FALLBACK_PORT_RANGE",
    "DEFAULT_UPSTREAM_IDLE_TIMEOUT_S",
    "REWRITE_BUFFER_LIMIT_BYTES",
    "UPSTREAM_CONNECT_TIMEOUT_S",
    "SERVICE_NAME",
    "LAST_PORT_BASENAME",
    "GatewayConfig",
    "HostNotLoopbackError",
    "credentials_path",
    "explicit_port",
    "export_path",
    "is_loopback",
    "last_port_path",
    "log_path",
    "pid_path",
    "port_candidates",
    "port_path",
    "resolve_host",
    "resolve_port",
    "token_path",
]
