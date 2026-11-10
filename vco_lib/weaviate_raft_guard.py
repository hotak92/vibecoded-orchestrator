# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Weaviate raft snapshots and the 1.37 "orphan drop" (v0.2.106).

THE DEFECT (upstream, read in the v1.37.18 source; user-facing write-up:
docs/TROUBLESHOOTING.md "Weaviate collections lose their data at every restart")
---------------------------------------------------------------------------
Weaviate keeps its schema in a raft log. A small install never takes a raft
snapshot (``RAFT_SNAPSHOT_THRESHOLD`` defaults to 8192 entries), so every
start replays the WHOLE log. From 1.37 a replayed ``DELETE_CLASS`` records the
class as an orphan, and at the end of the start every orphan not "revived" by
a later ``ADD_CLASS`` has its data dropped. The revived check compares names
case-SENSITIVELY, while the index and its folder are named by the lower-cased
class name. So ``DELETE_CLASS ProjectX_KnowledgeGraph`` followed by
``ADD_CLASS ProjectX_Knowledgegraph`` drops the LIVE class's data — at the
first start of 1.37 on such a log, and again at every start after it.
Weaviate 1.28 has no orphan drop; a raft snapshot ends the replay (a start
restores the snapshot, which records no orphans, and replays only the entries
after it).

WHAT THIS MODULE DOES (one home for every rule above)
-----------------------------------------------------
* :data:`COMPOSE_SNAPSHOT_ENV` — the snapshot settings the shipped compose
  carries (pinned by a parity test against ``infrastructure/docker-compose.yml``).
* :func:`upgrade_check` — called by :func:`vco_lib.data_identity.guard_recreate`
  (install.py step 5, ``service_lifecycle up`` and the port move) and by
  ``service_adoption.adopt_services`` (``hand-to-vco``) before a Weaviate
  container is (re)created. When the recreate MOVES data from a release
  without the orphan drop (<= 1.28) to one with it, it first takes a raft
  snapshot with the OLD image (:func:`snapshot_with_container_image`): the old
  container is stopped, a one-off container of the SAME image runs on the
  SAME volumes with a snapshot threshold of 1, VCO waits until the snapshot
  covers the whole log, removes the one-off and starts the old container
  again. When that cannot be done — or the version that last ran on the data
  is not known — the recreate is REFUSED (:data:`CID_UPGRADE_REFUSED`); VCO
  never starts a release with the orphan drop on data whose snapshot state it
  does not know.
* :func:`scan` + the doctor probes — a live Weaviate whose log is not covered
  by a snapshot while VCO's records show a case-variant class history
  (:data:`CID_REPLAY_EXPOSED`), classes present in the schema without their
  index (:data:`CID_INDEX_MISSING`, the already-damaged install) and a
  Weaviate container without a memory limit (:data:`CID_UNCAPPED`). All three
  are reported with the exact repair order; nothing here repairs or recreates
  anything by itself.

CLI::

    python -m vco_lib.weaviate_raft_guard status [--url URL] [--json]
    python -m vco_lib.weaviate_raft_guard snapshot --container NAME [--runtime podman|docker]
    python -m vco_lib.weaviate_raft_guard prepare [--root PATH] [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

__all__ = [
    "COMPOSE_SNAPSHOT_ENV",
    "ONE_OFF_SNAPSHOT_ENV",
    "CID_UPGRADE_REFUSED",
    "CID_REPLAY_EXPOSED",
    "CID_INDEX_MISSING",
    "CID_UNCAPPED",
    "RaftStats",
    "UpgradeVerdict",
    "parse_version",
    "has_orphan_drop",
    "parse_raft_stats",
    "upgrade_check",
    "snapshot_with_container_image",
    "scan",
]

#: The settings ``infrastructure/docker-compose.yml`` ships for the weaviate
#: service (MUST MATCH that file; tests/test_v02106_weaviate_raft_guard.py
#: parses it): a snapshot after every schema change, checked every 30-60 s.
COMPOSE_SNAPSHOT_ENV: dict[str, str] = {
    "RAFT_SNAPSHOT_THRESHOLD": "1",
    "RAFT_SNAPSHOT_INTERVAL": "30",
}
#: The one-off pre-upgrade run: snapshot as soon as raft checks (1-2 s).
ONE_OFF_SNAPSHOT_ENV: dict[str, str] = {
    "RAFT_SNAPSHOT_THRESHOLD": "1",
    "RAFT_SNAPSHOT_INTERVAL": "1",
}

#: The newest release line verified to have NO orphan drop (1.28.4 source).
#: 1.37.18 has it; the lines in between were not read, so they are treated
#: as having it (the conservative side: a needless snapshot costs a minute).
LAST_LINE_WITHOUT_ORPHAN_DROP = (1, 28)

CID_UPGRADE_REFUSED = "weaviate_upgrade_snapshot_refused"
CID_REPLAY_EXPOSED = "weaviate_raft_replay_exposed"
CID_INDEX_MISSING = "weaviate_collection_index_missing"
CID_UNCAPPED = "weaviate_container_uncapped"

#: The user's statement that the data needs no snapshot (they took one with
#: the old version themselves, or the data was already served by a release
#: with the orphan drop). Read only where VCO cannot tell the version that
#: wrote the data: a container whose version is not known, or a data folder
#: with raft history and no container at all. Never consulted when VCO can
#: take the snapshot itself.
CONFIRM_ENV = "VCT_WEAVIATE_UPGRADE_SNAPSHOT_CONFIRMED"
#: Seconds the one-off old-version run may take to start and snapshot. Long
#: by design: a large data folder loads for minutes on a slow disk.
TIMEOUT_ENV = "VCT_WEAVIATE_SNAPSHOT_TIMEOUT_S"
DEFAULT_TIMEOUT_S = 1800.0
#: The one-off container's name suffix (``<container>-raft-snapshot``).
ONE_OFF_SUFFIX = "-raft-snapshot"
#: The class the one-off creates and deletes so raft has a command to apply
#: after the start's configuration entry (see :func:`_write_marker`).
MARKER_CLASS = "VcoRaftSnapshotMarker"
#: Where the data lives inside the container (compose's PERSISTENCE_DATA_PATH).
DEFAULT_DATA_PATH = "/var/lib/weaviate"

NOT_NEEDED = "not_needed"
PREPARED = "prepared"
REFUSED = "refused"
#: :func:`pinned_target`'s answer when VCO's compose (read and parsed) runs
#: no Weaviate image at all — nothing it starts can open the data.
NO_IMAGE = "<compose runs no weaviate image>"

RunFn = Callable[..., "subprocess.CompletedProcess[str]"]
LogFn = Callable[[str], None]
HttpFn = Callable[[str, float], "tuple[int, bytes]"]

_KG_NODE = "docs/TROUBLESHOOTING.md"  # "Weaviate collections lose their data at every restart"
_CLI = "python -m vco_lib.weaviate_raft_guard"

_VERSION_RE = re.compile(r"(?<![\w.])v?(\d+)\.(\d+)(?:\.(\d+))?(?![\w.])")


# ─── versions ───────────────────────────────────────────────────────────


def parse_version(text: Optional[str]) -> Optional[tuple[int, int, int]]:
    """``(major, minor, patch)`` from an image reference's TAG
    (``cr.weaviate.io/semitechnologies/weaviate:1.37.18``, a digest suffix
    ignored) or a bare version (``1.28.4`` from ``/v1/meta``); ``None`` when
    the text names no version (``latest``, an image id)."""
    if not text:
        return None
    ref = str(text).strip().split("@", 1)[0]
    if "/" in ref or ":" in ref:
        tail = ref.rsplit("/", 1)[-1]
        if ":" not in tail:
            return None
        ref = tail.rsplit(":", 1)[1]
    m = _VERSION_RE.fullmatch(ref.strip()) or _VERSION_RE.match(ref.strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)


def has_orphan_drop(version: Optional[tuple[int, int, int]]) -> bool:
    """True when *version* may drop orphaned classes on replay (anything past
    the 1.28 line; an unknown version counts as True)."""
    if version is None:
        return True
    return tuple(version[:2]) > LAST_LINE_WITHOUT_ORPHAN_DROP


# ─── raft statistics (/v1/cluster/statistics) ───────────────────────────


@dataclass(frozen=True)
class RaftStats:
    last_log_index: int
    last_snapshot_index: int
    applied_index: int

    @property
    def covered(self) -> bool:
        """Every log entry is inside the latest snapshot: a start replays
        nothing the snapshot does not already hold."""
        return self.last_snapshot_index >= self.last_log_index


def parse_raft_stats(payload: Any) -> Optional[RaftStats]:
    """The local node's raft indexes from a ``/v1/cluster/statistics`` body
    (Weaviate prints them as strings), else ``None``."""
    try:
        stats = payload["statistics"]
        raft = (stats[0] if isinstance(stats, list) else stats)["raft"]
        return RaftStats(int(raft["lastLogIndex"]), int(raft["lastSnapshotIndex"]),
                         int(raft.get("appliedIndex") or 0))
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def _default_http(url: str, timeout: float) -> tuple[int, bytes]:
    from vco_lib import weaviate_helpers as _wh  # noqa: PLC0415

    return _wh.http_request("GET", url, timeout=timeout)


def _get_json(url: str, http: Optional[HttpFn], timeout: float = 10.0) -> Optional[Any]:
    try:
        status, body = (http or _default_http)(url, timeout)
    except Exception:  # noqa: BLE001 — could not look is not a verdict
        return None
    if status != 200:
        return None
    try:
        return json.loads(body or b"null")
    except ValueError:
        return None


def read_raft_stats(url: str, *, http: Optional[HttpFn] = None) -> Optional[RaftStats]:
    return parse_raft_stats(_get_json(f"{url.rstrip('/')}/v1/cluster/statistics", http))


def read_server_version(url: str, *, http: Optional[HttpFn] = None) -> Optional[tuple[int, int, int]]:
    meta = _get_json(f"{url.rstrip('/')}/v1/meta", http)
    return parse_version(meta.get("version")) if isinstance(meta, dict) else None


# ─── the pre-upgrade snapshot (work item 2) ─────────────────────────────


@dataclass
class UpgradeVerdict:
    status: str
    reason: str = ""
    lines: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status != REFUSED


def timeout_s(environ: Optional[Mapping[str, str]] = None) -> float:
    env = os.environ if environ is None else environ
    try:
        value = float(env.get(TIMEOUT_ENV, "") or DEFAULT_TIMEOUT_S)
    except ValueError:
        return DEFAULT_TIMEOUT_S
    return value if value > 0 else DEFAULT_TIMEOUT_S


def _run(run: RunFn, argv: Sequence[str], timeout: float = 120.0
         ) -> Optional["subprocess.CompletedProcess[str]"]:
    try:
        return run(list(argv), capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return None


def _inspect(ref: str, fmt: str, runtime: str, run: RunFn) -> Optional[str]:
    res = _run(run, [runtime, "inspect", "--type", "container", "--format", fmt, ref], 30)
    if res is None or res.returncode != 0:
        return None
    out = (res.stdout or "").strip()
    return out or None


def _exec_get(ref: str, path: str, runtime: str, run: RunFn) -> Optional[Any]:
    """GET ``http://127.0.0.1:8080<path>`` from INSIDE the container (busybox
    wget, present in every Weaviate image) — no host port needed."""
    res = _run(run, [runtime, "exec", ref, "wget", "-q", "-T", "5", "-O", "-",
                     f"http://127.0.0.1:8080{path}"], 30)
    if res is None or res.returncode != 0:
        return None
    try:
        return json.loads(res.stdout or "null")
    except ValueError:
        return None


def container_version(ref: str, runtime: str, run: RunFn) -> Optional[tuple[int, int, int]]:
    """The Weaviate version a container runs: its image tag, else (running
    only) the server's own ``/v1/meta``. ``None`` = not known."""
    version = parse_version(_inspect(ref, "{{.Config.Image}}", runtime, run))
    if version is not None:
        return version
    meta = _exec_get(ref, "/v1/meta", runtime, run)
    return parse_version(meta.get("version")) if isinstance(meta, dict) else None


def _sleep(seconds: float) -> None:
    """The poll wait (module-level so a test can replace it)."""
    time.sleep(seconds)


def _monotonic() -> float:
    return time.monotonic()


def _method_http(method: str, url: str, body: Optional[dict] = None,
                 timeout: float = 15.0) -> tuple[int, bytes]:
    """One REST call to the one-off (module-level so a test can replace it)."""
    from vco_lib import weaviate_helpers as _wh  # noqa: PLC0415

    return _wh.http_request(method, url, body=body, timeout=timeout)


def _published_port(name: str, runtime: str, run: RunFn) -> Optional[str]:
    """The loopback host port ``<runtime> port`` reports for 8080/tcp."""
    res = _run(run, [runtime, "port", name, "8080/tcp"], 30)
    if res is None or res.returncode != 0:
        return None
    for line in (res.stdout or "").splitlines():
        port = line.strip().rsplit(":", 1)[-1]
        if port.isdecimal():
            return port
    return None


def _one_off_stats(base: str) -> Optional[RaftStats]:
    try:
        status, body = _method_http("GET", f"{base}/v1/cluster/statistics")
        return parse_raft_stats(json.loads(body or b"null")) if status == 200 else None
    except Exception:  # noqa: BLE001 — still starting
        return None


def _write_marker(base: str) -> Optional[str]:
    """Create and delete :data:`MARKER_CLASS` — two schema COMMANDS. hashicorp
    raft refuses a snapshot while the FSM has not applied the configuration
    entry a start appends ("cannot take snapshot now, wait until the
    configuration entry at N has been applied", measured on 1.28.4), and on an
    idle node nothing would ever be applied after it. Both commands land in
    the snapshot, so neither is ever replayed.

    ``None`` on success, else the refusal reason — which names a marker class
    left in the schema when the create landed and the delete did not (review
    A-L2; the next attempt removes it through the 422 path)."""
    created = False
    try:
        status, _ = _method_http("POST", f"{base}/v1/schema",
                                 body={"class": MARKER_CLASS, "vectorizer": "none"})
        if status not in (200, 422):  # 422: a leftover marker from an interrupted run
            return (f"the one-off snapshot container did not accept the marker class "
                    f"{MARKER_CLASS} (HTTP {status})")
        created = True
        status, _ = _method_http("DELETE", f"{base}/v1/schema/{MARKER_CLASS}")
        if status in (200, 204):
            return None
        detail = f"HTTP {status}"
    except Exception as exc:  # noqa: BLE001
        detail = f"{type(exc).__name__}: {exc}"
        if not created:
            return (f"the one-off snapshot container did not accept the marker class "
                    f"{MARKER_CLASS} ({detail})")
    return (f"the marker class {MARKER_CLASS} was created but its delete failed ({detail}); "
            f"an empty {MARKER_CLASS} class may remain in the schema — the next snapshot "
            "attempt removes it")


def snapshot_with_container_image(
    ref: str,
    *,
    runtime: str,
    run: RunFn,
    log: Optional[LogFn] = None,
    sleep: Optional[Callable[[float], None]] = None,
    monotonic: Optional[Callable[[], float]] = None,
    timeout: Optional[float] = None,
    poll_s: float = 2.0,
) -> UpgradeVerdict:
    """Take a raft snapshot of container *ref*'s data with *ref*'s OWN image.

    *ref* is stopped (two processes must never open one data folder). A
    one-off container of the same image runs with ``--volumes-from ref``, the
    container's own environment plus :data:`ONE_OFF_SNAPSHOT_ENV`, its own
    memory limit and a loopback-only random host port. Once it answers, VCO
    writes the marker commands (:func:`_write_marker`), reads the raft log's
    last index, and waits until ``lastSnapshotIndex`` reaches it — the whole
    log is then inside the snapshot. The one-off is removed and *ref* started
    again when it was running, so the machine is left as it was found plus
    the snapshot. Any failure is a ``refused`` verdict naming the step."""
    say = log or (lambda _m: None)
    sleep_fn, clock = sleep or _sleep, monotonic or _monotonic
    deadline_s = timeout if timeout is not None else timeout_s()
    verdict = UpgradeVerdict(REFUSED)
    image = _inspect(ref, "{{.Image}}", runtime, run)
    env_raw = _inspect(ref, "{{json .Config.Env}}", runtime, run)
    running = _inspect(ref, "{{.State.Running}}", runtime, run)
    memory = _inspect(ref, "{{.HostConfig.Memory}}", runtime, run)
    try:
        env = json.loads(env_raw or "null")
    except ValueError:
        env = None
    if not image or not isinstance(env, list) or running not in ("true", "false"):
        verdict.reason = (f"container '{ref}' could not be inspected (image, environment and "
                          "state are needed to run its own version once on the same data)")
        return verdict
    was_running = running == "true"
    one_off = f"{ref}{ONE_OFF_SUFFIX}"
    _run(run, [runtime, "rm", "-f", one_off], 60)  # only VCO's own one-off name
    if was_running:
        say(f"  [raft] stopping {ref} to take a schema snapshot with its own image")
        res = _run(run, [runtime, "stop", "-t", "120", ref], 180)
        if res is None or res.returncode != 0:
            verdict.reason = f"container '{ref}' could not be stopped for the snapshot run"
            return verdict
    restart_note = ""
    try:
        argv = [runtime, "run", "-d", "--name", one_off, "--volumes-from", ref,
                "-p", "127.0.0.1::8080"]
        if memory and memory.isdecimal() and int(memory) > 0:
            argv += ["--memory", memory]  # the old container's own limit
        for item in env:
            if isinstance(item, str) and item.partition("=")[0] not in ONE_OFF_SNAPSHOT_ENV:
                argv += ["-e", item]
        for key, value in ONE_OFF_SNAPSHOT_ENV.items():
            argv += ["-e", f"{key}={value}"]
        argv.append(image)
        res = _run(run, argv, 300)
        port = _published_port(one_off, runtime, run) if res is not None and res.returncode == 0 \
            else None
        if port is None:
            tail = ((res.stderr or "").strip().splitlines()[-1:] if res is not None else [])
            verdict.reason = ("the one-off snapshot container could not start"
                              + (f": {tail[0]}" if tail else ""))
            return verdict
        base = f"http://127.0.0.1:{port}"
        target: Optional[int] = None
        start = clock()
        while True:
            stats = _one_off_stats(base)
            if stats is None and _inspect(one_off, "{{.State.Running}}", runtime, run) != "true":
                logs = _run(run, [runtime, "logs", "--tail", "3", one_off], 30)
                tail = ((logs.stdout or "") + (logs.stderr or "")).strip().splitlines()[-1:] \
                    if logs is not None else []
                verdict.reason = ("the one-off snapshot container stopped before it answered"
                                  + (f": {tail[0][:300]}" if tail else ""))
                return verdict
            if stats is not None and target is None:
                marker_error = _write_marker(base)
                if marker_error is not None:
                    verdict.reason = marker_error
                    return verdict
                stats = _one_off_stats(base)
                target = stats.last_log_index if stats is not None else None
            if stats is not None and target is not None and stats.last_snapshot_index >= target:
                verdict.status = PREPARED
                verdict.reason = (f"raft snapshot at index {stats.last_snapshot_index} covers "
                                  f"the whole schema log (last index {target})")
                verdict.lines.append(f"  [raft] {ref}: {verdict.reason}")
                return verdict
            if clock() - start >= deadline_s:
                verdict.reason = (f"no raft snapshot covering the log within {int(deadline_s)} s "
                                  f"(set {TIMEOUT_ENV} for a slower disk)")
                return verdict
            sleep_fn(poll_s)
    finally:
        _run(run, [runtime, "stop", "-t", "60", one_off], 120)
        _run(run, [runtime, "rm", "-f", one_off], 60)
        if was_running:
            res = _run(run, [runtime, "start", ref], 180)
            if res is None or res.returncode != 0:
                restart_note = f"; container '{ref}' could NOT be started again — start it by hand"
        if restart_note:
            verdict.reason += restart_note
            verdict.lines.append(f"  [raft] WARNING{restart_note}")


def _folder_has_raft_log(mount: Optional[Mapping[str, Any]], runtime: str,
                         run: RunFn) -> Optional[bool]:
    """Does the data location compose would mount already hold a raft log?
    ``False`` for a location that does not exist yet; ``None`` = could not
    look. Read-only (host paths only — a named volume through its host
    mountpoint)."""
    if not mount:
        return None
    kind, source = str(mount.get("kind") or mount.get("type") or ""), str(mount.get("source") or "")
    if not source:
        return None
    if kind == "volume":
        res = _run(run, [runtime, "volume", "inspect", "--format", "{{.Mountpoint}}", source], 30)
        if res is None:
            return None
        if res.returncode != 0:
            err = (res.stderr or "").lower()
            return False if ("no such volume" in err or "not found" in err) else None
        mountpoint = (res.stdout or "").strip()
        if not mountpoint:
            return None
        root = Path(mountpoint)
    else:
        root = Path(source)
    try:
        if not root.exists():
            # A missing bind source holds nothing. A volume that EXISTS but
            # whose mountpoint this host cannot see (Docker Desktop keeps it in
            # its VM) is not known to be empty.
            return False if kind != "volume" else None
        return (root / "raft" / "raft.db").exists()
    except OSError:
        return None


def upgrade_check(
    *,
    service: str,
    live_state: str,
    ref: str,
    target_image: Optional[str],
    data_mount: "Optional[Mapping[str, Any]] | Callable[[], Optional[Mapping[str, Any]]]",
    runtime: str,
    run: RunFn,
    log: Optional[LogFn] = None,
    environ: Optional[Mapping[str, str]] = None,
    snapshot: Optional[Callable[..., UpgradeVerdict]] = None,
) -> UpgradeVerdict:
    """May *service*'s container be (re)created on *target_image*?

    ``live_state`` is :mod:`vco_lib.data_identity`'s ``present`` / ``absent``
    for the container *ref*; *data_mount* is the data location the new
    container will mount (or a callable producing it, called only when no
    container exists). Only Weaviate moving onto a release with the orphan
    drop is ever touched; see the module docstring for the rules."""
    if service != "weaviate":
        return UpgradeVerdict(NOT_NEEDED)
    if target_image == NO_IMAGE:
        return UpgradeVerdict(NOT_NEEDED, "VCO's compose runs no Weaviate image")
    target = parse_version(target_image)
    if target is not None and not has_orphan_drop(target):
        return UpgradeVerdict(NOT_NEEDED, f"target {target_image} has no orphan drop")
    env = os.environ if environ is None else environ
    confirmed = (env.get(CONFIRM_ENV) or "").strip() == "1"
    if live_state == "present" and ref:
        current = container_version(ref, runtime, run)
        if current is None and confirmed:
            return UpgradeVerdict(NOT_NEEDED, f"{CONFIRM_ENV}=1 (the user confirmed the snapshot)")
        if current is None:
            return UpgradeVerdict(REFUSED, (
                f"the Weaviate version container '{ref}' runs is not known (no version in its "
                "image tag and no answer from /v1/meta), so whether its schema log needs a "
                "snapshot before the move cannot be decided"))
        if has_orphan_drop(current):
            return UpgradeVerdict(NOT_NEEDED, (
                f"'{ref}' already runs {'.'.join(map(str, current))}: a recreate on it is a "
                "restart, not a move"))
        return (snapshot or snapshot_with_container_image)(ref, runtime=runtime, run=run, log=log)
    mount = data_mount() if callable(data_mount) else data_mount
    has_log = _folder_has_raft_log(mount, runtime, run)
    if has_log is False:
        return UpgradeVerdict(NOT_NEEDED, "the data location holds no raft log yet")
    if confirmed:
        return UpgradeVerdict(NOT_NEEDED, f"{CONFIRM_ENV}=1 (the user confirmed the snapshot)")
    where = (f"{mount.get('kind') or mount.get('type')} {mount.get('source')}"
             if mount else "the data location")
    return UpgradeVerdict(REFUSED, (
        f"{where} {'holds' if has_log else 'may hold'} a Weaviate raft log but no container "
        "names the version that wrote it, so VCO cannot take a snapshot with that version "
        f"before starting {target_image or 'the pinned image'} on it"))


def _compose_cfg(root: Path) -> Optional[dict]:
    """The installer's effective compose config (base + managed overrides +
    ``infrastructure/.env``, merged in Python — no compose subprocess), or
    ``None`` when the base file cannot be read."""
    from vco_lib import service_adoption as _sa  # noqa: PLC0415

    infra = Path(root) / "infrastructure"
    _files, cfg = _sa._compose_entrypoints(
        False, None, infra, _sa.infrastructure_env_for_substitution(infra))
    return cfg if isinstance(cfg.get("services"), dict) else None


def pinned_target(root: Path) -> Optional[str]:
    """The Weaviate image VCO's compose under *root* would start;
    :data:`NO_IMAGE` when the readable compose defines none; ``None`` when the
    compose cannot be read (the caller then treats the target as affected)."""
    cfg = _compose_cfg(root)
    if cfg is None:
        return None
    image = (cfg["services"].get("weaviate") or {}).get("image")
    return image.strip() if isinstance(image, str) and image.strip() else NO_IMAGE


def compose_data_mount(root: Path) -> Optional[dict]:
    """The data location VCO's compose would mount at ``/var/lib/weaviate``
    (same Python merge as :func:`pinned_target`), as a row-shaped mount."""
    from vco_lib import service_adoption as _sa  # noqa: PLC0415

    cfg = _compose_cfg(root)
    if cfg is None:
        return None
    spec = _sa.config_mounts(cfg["services"].get("weaviate") or {},
                             cfg.get("volumes") or {}).get(DEFAULT_DATA_PATH)
    return None if spec is None else {"kind": spec.kind, "source": spec.source,
                                      "destination": spec.destination}


def refusal_entry(service: str, ref: str, reason: str, *, runtime: str = "",
                  target_image: Optional[str] = None,
                  manual_cmd: str = "python install.py --update"):
    """The :data:`CID_UPGRADE_REFUSED` ledger row (one builder)."""
    from vco_lib.deferral_report import DeferralEntry  # noqa: PLC0415

    rt = runtime or "<podman|docker>"
    name = ref or "<container>"
    return DeferralEntry(
        condition_id=CID_UPGRADE_REFUSED,
        title=f"{service}: not moved to {target_image or 'the pinned Weaviate'} — "
              "no raft snapshot of its schema",
        detected=f"{service}: {reason}.",
        why_deferred=(
            "From Weaviate 1.37 a start that replays a schema log containing a class delete "
            "can drop the data of a LIVE class whose name differs only in letter case "
            f"({_KG_NODE}). A raft snapshot taken by the OLD version first prevents it. VCO "
            "takes that snapshot itself when the old container exists; here it could not, so "
            "the container was left as it is — nothing was stopped for good, removed or "
            "recreated."),
        command_to_apply=(
            f"{_CLI} status            # version + raft snapshot coverage of the running Weaviate\n"
            f"{_CLI} snapshot --container {name} --runtime {rt}\n"
            "#   ^ runs the container's OWN image once on its data until a snapshot covers the "
            "log, then starts it again\n"
            "# when VCO cannot tell the version that wrote the data (a stopped container with no\n"
            "# version in its image tag, or data with no container at all): once the snapshot\n"
            "# above was taken (or the data was already served by Weaviate >= 1.29), set\n"
            f"# {CONFIRM_ENV}=1 for ONE run of:\n"
            f"{manual_cmd}"),
        severity="warning",
        disposition="action_required",
        dismiss_fields={"service": service, "container": name},
        kg_node_refs=[_KG_NODE],
    )


# ─── detection (work items 3 and 5) ─────────────────────────────────────


@dataclass
class RaftScan:
    """One read of a live Weaviate (``None`` fields = could not read)."""

    url: str
    version: Optional[tuple[int, int, int]] = None
    stats: Optional[RaftStats] = None
    classes: list[str] = field(default_factory=list)
    #: classes in the schema whose index is gone ("non-existing index")
    index_missing: list[str] = field(default_factory=list)
    #: (name VCO derived or deleted, live class) pairs that differ only in case
    case_pairs: list[tuple[str, str]] = field(default_factory=list)
    #: the raft log index of each recorded name's delete, when VCO recorded it
    #: (a derived name, or a delete recorded without statistics: ``None``)
    recorded_index: dict[str, Optional[int]] = field(default_factory=dict)

    @property
    def exposed(self) -> bool:
        """A start can replay a case-variant delete: a recorded delete whose
        index lies after the latest snapshot. Not "the log is longer than the
        snapshot": every start appends a no-op and a configuration entry that
        no snapshot can hold until the next schema command, and those replay
        harmlessly.

        A delete whose index VCO never recorded (the root's derived names,
        deleted before the ledger existed) gets an UPPER BOUND in :func:`scan`:
        the log index at the first reading that saw the pair, persisted, since
        the delete had already happened by then (review A-L1: "no snapshot at
        all" alone read a machine whose only snapshot predates the delete as
        safe). The ``None`` branch below is for a scan that could not bound it."""
        if self.stats is None:
            return False
        snap = self.stats.last_snapshot_index
        for recorded, _live in self.case_pairs:
            index = self.recorded_index.get(recorded)
            if (index is not None and index > snap) or (index is None and snap == 0):
                return True
        return False

    def to_dict(self) -> dict:
        d = asdict(self)
        d["exposed"] = self.exposed
        d["covered"] = self.stats.covered if self.stats is not None else None
        return d


def index_missing(url: str, cls: str, *, http: Optional[HttpFn] = None) -> Optional[bool]:
    """True when *cls* is in the schema but Weaviate has no index for it (the
    orphan drop's footprint): its shard status answers "non-existing index"
    (measured on 1.37.18 after the drop: HTTP 404 ``cannot get shards status
    for a non-existing index``; searches report "tried to browse non-existing
    index"). ``None`` = could not tell."""
    try:
        status, body = (http or _default_http)(
            f"{url.rstrip('/')}/v1/schema/{cls}/shards", 15.0)
    except Exception:  # noqa: BLE001
        return None
    if status == 200:
        return False
    text = (body or b"").decode("utf-8", "replace").lower()
    return True if "non-existing index" in text else None


def case_variant_pairs(live: Iterable[str], recorded: Iterable[str]) -> list[tuple[str, str]]:
    """``(recorded, live)`` pairs whose names differ ONLY in case. *recorded*
    is VCO's evidence of names it created or deleted (derived names, the
    deleted-class ledger)."""
    by_lower: dict[str, list[str]] = {}
    for name in live:
        by_lower.setdefault(name.lower(), []).append(name)
    pairs = []
    for name in dict.fromkeys(recorded):
        for other in by_lower.get(name.lower(), []):
            if other != name:
                pairs.append((name, other))
    return sorted(set(pairs))


def recorded_names(root: Optional[Path], url: str) -> dict[str, Optional[int]]:
    """VCO's own records of class names it derived or deleted: the root
    folder's derived KG / Development / Diagrams names (the v0.2.88 case
    rebind dropped one spelling and created the other) and the deleted-class
    ledger (:mod:`vco_lib.weaviate_case_history`), each with the raft log
    index of its delete when known."""
    names: dict[str, Optional[int]] = {}
    if root is not None:
        try:
            from vco_lib.project_init import derive_project_collection_names  # noqa: PLC0415

            derived = derive_project_collection_names(Path(root).name or "")
            names.update({str(v): None for v in derived.values() if isinstance(v, str) and v})
        except Exception:  # noqa: BLE001 — evidence is best-effort
            pass
    try:
        from vco_lib import weaviate_case_history as _wch  # noqa: PLC0415

        names.update(_wch.deleted_entries(url))
    except Exception:  # noqa: BLE001
        pass
    return names


def scan(url: str, *, root: Optional[Path] = None, http: Optional[HttpFn] = None,
         check_indexes: bool = True) -> Optional[RaftScan]:
    """Read version, raft indexes and the class list of the Weaviate at
    *url*, match the class list against VCO's case-variant records and
    (optionally) check the index of each LIVE class of such a pair — the
    only classes the orphan drop can hit through VCO's history. Only those:
    a shard-status read can load a lazily-unloaded shard, and loading every
    shard of every class is the memory spike VCO's lazy loading exists to
    avoid. ``None`` when the schema could not be read at all."""
    from vco_lib import weaviate_helpers as _wh  # noqa: PLC0415

    schema = _get_json(f"{url.rstrip('/')}/v1/schema", http)
    if not isinstance(schema, dict):
        return None
    result = RaftScan(url=url, version=read_server_version(url, http=http),
                      stats=read_raft_stats(url, http=http),
                      classes=sorted(_wh.schema_class_names(schema)))
    recorded = recorded_names(root, url)
    result.case_pairs = case_variant_pairs(result.classes, recorded)
    result.recorded_index = {a: recorded.get(a) for a, _b in result.case_pairs}
    if result.stats is not None:
        unknown = [a for a, idx in result.recorded_index.items() if idx is None]
        if unknown:
            from vco_lib import weaviate_case_history as _wch  # noqa: PLC0415

            result.recorded_index.update(_wch.delete_index_bounds(
                url, unknown, result.stats.last_log_index))
    if check_indexes:
        live = sorted({b for _a, b in result.case_pairs})
        result.index_missing = [c for c in live if index_missing(url, c, http=http) is True]
    return result


def _weaviate_url() -> str:
    from vco_lib import weaviate_helpers as _wh  # noqa: PLC0415

    return _wh.weaviate_url_default()


def _is_install_root(folder: Path) -> bool:
    return (Path(folder) / "vco_lib" / "__init__.py").is_file()


def repair_order(classes: Sequence[str], url: str) -> str:
    """The repair for classes whose index the orphan drop removed — in the
    only order that does not lose the rebuild again."""
    lines = [
        "# 1. make sure the running Weaviate takes raft snapshots: its container must carry",
        "#    RAFT_SNAPSHOT_THRESHOLD/RAFT_SNAPSHOT_INTERVAL (the shipped compose does;",
        "#    `python install.py --update` recreates a VCO-managed container with them):",
        f"{_CLI} status   # version and raft indexes",
        "# 2. drop each broken class (its data is already gone) and create it again:",
    ]
    lines += [f"curl -X DELETE {url.rstrip('/')}/v1/schema/{c}" for c in classes]
    lines += [
        "python install.py --update   # the orchestrator root re-creates its missing collections",
        "# any other project: python -m vco_lib.project_init bootstrap-collections \\",
        "#   --name \"<project name>\" --project-folder <project folder>",
        "# 3. re-fill it from its source files (KG: knowledge/, Development: docs/):",
        ".claude/scripts/kg-sync --all",
        f"# 4. before ANY restart, wait until `{_CLI} status` prints 'covered: yes'",
        "#    (the class creates in step 2 make the next snapshot cover the log)",
    ]
    return "\n".join(lines)


def probe_raft(folder: Path, res: Any, ctx: dict) -> list:
    """Doctor probe ``weaviate_raft`` (install roots only): the
    :data:`CID_INDEX_MISSING` and :data:`CID_REPLAY_EXPOSED` findings, from
    ONE :func:`scan` (``res.resolve_weaviate_raft_scan``). A clean reading is
    an OK finding carrying the cid, so the reading that emitted an entry is
    the one that clears it."""
    from vco_lib import doctor as d  # noqa: PLC0415

    if not _is_install_root(folder):
        return []
    result = res.resolve_weaviate_raft_scan(Path(folder))
    if result is None:
        return [d.Finding(probe="weaviate_raft", status=d.STATUS_UNKNOWN,
                          summary="Weaviate schema could not be read — raft state unknown.")]
    findings = []
    detail = result.to_dict()
    if result.index_missing:
        findings.append(d.Finding(
            probe="weaviate_raft", status=d.STATUS_PROBLEM, fix=d.FIX_DEFER,
            condition_id=CID_INDEX_MISSING,
            summary=(f"{len(result.index_missing)} Weaviate class(es) are in the schema but "
                     f"their data index is gone: {', '.join(result.index_missing)}"),
            command=repair_order(result.index_missing, result.url),
            detail=dict(detail, classes=list(result.index_missing))))
    else:
        findings.append(d.Finding(probe="weaviate_raft", status=d.STATUS_OK,
                                  condition_id=CID_INDEX_MISSING,
                                  summary="every Weaviate class has its data index"))
    if result.exposed:
        pairs = ", ".join(f"{a} / {b}" for a, b in result.case_pairs)
        stats = result.stats
        assert stats is not None  # exposed implies stats
        findings.append(d.Finding(
            probe="weaviate_raft", status=d.STATUS_PROBLEM, fix=d.FIX_DEFER,
            condition_id=CID_REPLAY_EXPOSED,
            summary=(f"Weaviate's schema log is not covered by a raft snapshot (snapshot "
                     f"{stats.last_snapshot_index} < log {stats.last_log_index}) while class names "
                     f"differ only in case: {pairs}"),
            command=exposure_remedy(result),
            detail=dict(detail, pairs=[list(p) for p in result.case_pairs])))
    elif result.stats is not None:
        findings.append(d.Finding(probe="weaviate_raft", status=d.STATUS_OK,
                                  condition_id=CID_REPLAY_EXPOSED,
                                  summary=("raft snapshot covers the schema log"
                                           if result.stats.covered else
                                           "no case-variant class history recorded")))
    return findings


def exposure_remedy(result: RaftScan) -> str:
    version = result.version
    if version is not None and not has_orphan_drop(version):
        return "\n".join([
            f"# This Weaviate ({'.'.join(map(str, version))}) has no orphan drop; the danger is "
            "the FIRST start of 1.37 on this data.",
            "# VCO's own moves (python install.py --update, service_endpoints hand-to-vco) take",
            "# a snapshot with this version first. Before moving it to Weaviate >= 1.29 by",
            "# hand, take one with its own image:",
            f"{_CLI} snapshot --container <its container>",
        ])
    return "\n".join([
        "# The NEXT start of this Weaviate can drop the data of the classes named above.",
        "# Recreate it with the shipped snapshot settings (VCO-managed: python install.py "
        "--update),",
        "# then check the classes and follow the repair order if any lost their index:",
        f"{_CLI} status",
        "python -m vco_lib.doctor",
    ])


def _entry(cid: str, title: str, finding: Any, why: str, dismiss: dict):
    from vco_lib.deferral_report import DeferralEntry  # noqa: PLC0415

    return DeferralEntry(
        condition_id=cid, title=title, detected=finding.summary, why_deferred=why,
        command_to_apply=finding.command, severity="warning", disposition="action_required",
        dismiss_fields=dismiss, kg_node_refs=[_KG_NODE])


def index_missing_entry(finding: Any):
    return _entry(
        CID_INDEX_MISSING, "Weaviate classes lost their data index", finding,
        ("Weaviate 1.37's start-up orphan drop removed the index of these classes (a "
         "replayed class delete whose name differs only in case from the live class). "
         "Rebuilding them is destructive and must follow the order shown — a rebuild "
         "before the raft snapshot exists is wiped again at the next restart — so VCO does "
         "not do it on its own. The entry clears itself once every class has its index."),
        {"classes": sorted((getattr(finding, "detail", None) or {}).get("classes") or [])})


def replay_exposed_entry(finding: Any):
    return _entry(
        CID_REPLAY_EXPOSED, "Weaviate start can drop a live class (case-variant history)",
        finding,
        ("VCO's records show class names that differ only in letter case, and Weaviate's "
         "schema log is not inside a raft snapshot, so every start replays it. From 1.37 "
         "that replay can drop the live class's data. The entry clears itself once a "
         "snapshot covers the log."),
        {"pairs": [list(p) for p in (getattr(finding, "detail", None) or {}).get("pairs") or []]})


def still_index_missing(url: Optional[str] = None) -> Optional[bool]:
    """Registry clear probe for :data:`CID_INDEX_MISSING`."""
    result = scan(url or _weaviate_url())
    return None if result is None else bool(result.index_missing)


def still_exposed(url: Optional[str] = None, root: Optional[Path] = None) -> Optional[bool]:
    """Registry clear probe for :data:`CID_REPLAY_EXPOSED`."""
    result = scan(url or _weaviate_url(), root=root, check_indexes=False)
    if result is None or result.stats is None:
        return None
    return result.exposed


# ─── the uncapped container (work item 5) ───────────────────────────────


@dataclass(frozen=True)
class MemCap:
    container: str
    limit_bytes: Optional[int]
    image: str = ""

    @property
    def uncapped(self) -> bool:
        return self.limit_bytes == 0


def read_mem_cap(runtime: str, *, run: Optional[RunFn] = None,
                 names: Optional[Sequence[str]] = None) -> Optional[MemCap]:
    """The running Weaviate container's memory limit (``HostConfig.Memory``,
    0 = none); ``None`` when no container was found or it could not be read."""
    from vco_lib import containers as _containers  # noqa: PLC0415
    from vco_lib import tool_search_dirs as _tsd  # noqa: PLC0415

    _run_fn = run or _tsd.run
    candidates = list(names or _containers.all_known_names("weaviate"))
    for name in candidates:
        raw = _inspect(name, "{{.HostConfig.Memory}}", runtime, _run_fn)
        if raw is None:
            continue
        try:
            limit = int(raw)
        except ValueError:
            return MemCap(name, None)
        return MemCap(name, limit, _inspect(name, "{{.Config.Image}}", runtime, _run_fn) or "")
    return None


def uncapped_remedy(cap: MemCap, runtime: str, limit: Optional[str]) -> str:
    value = limit or "<VCT_WEAVIATE_MEM_LIMIT from infrastructure/.env, e.g. 8g>"
    return "\n".join([
        "# Cap it WITHOUT recreating (the kernel limit applies at once; Weaviate's own",
        "# soft limit follows on its next start):",
        f"{runtime} update --memory {value} --memory-swap {value} {cap.container}",
        "# A recreate also applies it. Recreate ONLY through VCO (python install.py --update for",
        "# a VCO-managed container, python -m vco_lib.service_endpoints hand-to-vco --service",
        "# weaviate for an adopted one): both take a raft snapshot with the current version",
        "# first when the recreate moves it to Weaviate >= 1.29. Never recreate it by hand onto",
        "# Weaviate >= 1.29 without taking that snapshot first:",
        f"{_CLI} snapshot --container {cap.container}",
    ])


def probe_mem_cap(folder: Path, res: Any, ctx: dict) -> list:
    """Doctor probe ``weaviate_mem_cap`` (install roots only)."""
    from vco_lib import doctor as d  # noqa: PLC0415

    if not _is_install_root(folder):
        return []
    found = res.resolve_weaviate_mem_cap()
    if found is None:
        return []
    cap, runtime, limit = found
    if cap.limit_bytes is None:
        return [d.Finding(probe="weaviate_mem_cap", status=d.STATUS_UNKNOWN,
                          summary=f"memory limit of {cap.container} could not be read")]
    if not cap.uncapped:
        return [d.Finding(probe="weaviate_mem_cap", status=d.STATUS_OK,
                          condition_id=CID_UNCAPPED,
                          summary=f"{cap.container} has a memory limit "
                                  f"({cap.limit_bytes // (1024 * 1024)} MiB)")]
    return [d.Finding(
        probe="weaviate_mem_cap", status=d.STATUS_PROBLEM, fix=d.FIX_DEFER,
        condition_id=CID_UNCAPPED,
        summary=(f"{cap.container} runs without a memory limit: under memory pressure the "
                 "kernel kills it — or the desktop session — instead of Weaviate refusing a "
                 "write batch"),
        command=uncapped_remedy(cap, runtime, limit),
        detail={"container": cap.container, "image": cap.image})]


def uncapped_entry(finding: Any):
    detail = getattr(finding, "detail", None) or {}
    return _entry(
        CID_UNCAPPED, "Weaviate container has no memory limit", finding,
        ("VCO sizes Weaviate's limit from the host (VCT_WEAVIATE_MEM_LIMIT) only for "
         "containers it creates; a container it adopted keeps the limit it was created "
         "with — here none. VCO never recreates a running Weaviate on its own. The entry "
         "clears itself once the container has a limit."),
        {"container": str(detail.get("container") or "")})


def _runtime() -> Optional[str]:
    from vco_lib import containers as _containers  # noqa: PLC0415

    try:
        return _containers.resolve().runtime or None
    except Exception:  # noqa: BLE001 — no runtime is "could not look"
        return None


def default_mem_cap() -> Optional[tuple[MemCap, str, Optional[str]]]:
    """``(cap, runtime, the limit install sized)`` for the doctor, or None."""
    runtime = _runtime()
    if runtime is None:
        return None
    cap = read_mem_cap(runtime)
    if cap is None:
        return None
    limit: Optional[str] = None
    try:
        from vco_lib import service_adoption as _sa  # noqa: PLC0415
        from vco_lib import weaviate_memory as _wm  # noqa: PLC0415

        infra = Path(__file__).resolve().parent.parent / "infrastructure"
        value = (_sa.infrastructure_env_for_substitution(infra) or {}).get(_wm.ENV_KEY) or ""
        limit = value if value and value != "0" else None
    except Exception:  # noqa: BLE001 — the remedy then names the key instead
        limit = None
    return cap, runtime, limit


def default_scan(folder: Path) -> Optional[RaftScan]:
    try:
        return scan(_weaviate_url(), root=Path(folder))
    except Exception:  # noqa: BLE001 — could not look is not a verdict
        return None


def doctor_entry(finding: Any):
    """The ledger row for one of this module's doctor findings."""
    builders = {CID_INDEX_MISSING: index_missing_entry, CID_REPLAY_EXPOSED: replay_exposed_entry,
                CID_UNCAPPED: uncapped_entry}
    return builders[finding.condition_id](finding)


def still_uncapped() -> Optional[bool]:
    """Registry clear probe for :data:`CID_UNCAPPED`."""
    found = default_mem_cap()
    if found is None or found[0].limit_bytes is None:
        return None
    return found[0].uncapped


def prepare_for_compose(root: Path, *, runtime: Optional[str] = None,
                        run: Optional[RunFn] = None,
                        log: Optional[LogFn] = None) -> UpgradeVerdict:
    """:func:`upgrade_check` for the Weaviate container VCO's compose under
    *root* would (re)create — for a caller about to run that compose itself
    (the launcher's volume migration, through ``prepare``). Reads the live
    container the same way :func:`vco_lib.data_identity.guard_recreate` does."""
    from vco_lib import data_identity as _di  # noqa: PLC0415
    from vco_lib import tool_search_dirs as _tsd  # noqa: PLC0415

    rt = runtime or _runtime()
    if not rt:
        return UpgradeVerdict(REFUSED, "no container runtime was found to inspect Weaviate with")
    run_fn = run or _tsd.run
    live = _di.capture_live_mount("weaviate", rt, run=run_fn)
    if live.state == _di.LIVE_UNKNOWN:
        return UpgradeVerdict(REFUSED, f"the Weaviate container could not be inspected "
                                       f"({live.detail or 'runtime did not answer'})")
    return upgrade_check(service="weaviate", live_state=live.state, ref=live.ref,
                         target_image=pinned_target(Path(root)),
                         data_mount=live.mount or (lambda: compose_data_mount(Path(root))),
                         runtime=rt, run=run_fn, log=log)


# ─── CLI ────────────────────────────────────────────────────────────────


def _status(url: str, as_json: bool) -> int:
    result = scan(url, root=Path(__file__).resolve().parent.parent, check_indexes=True)
    if result is None:
        print(f"Weaviate at {url}: schema not readable", file=sys.stderr)
        return 2
    if as_json:
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        return 0
    stats = result.stats
    print(f"Weaviate {url}: version "
          f"{'.'.join(map(str, result.version)) if result.version else 'unknown'}")
    if stats is None:
        print("raft: statistics not readable")
    else:
        print(f"raft: last log index {stats.last_log_index}, last snapshot index "
              f"{stats.last_snapshot_index} — covered: {'yes' if stats.covered else 'no'}")
        print("replay of a recorded case-variant delete possible: "
              + ("YES" if result.exposed else "no"))
    print(f"classes: {len(result.classes)}; without index: "
          f"{', '.join(result.index_missing) or 'none'}")
    if result.case_pairs:
        print("case-variant names: " + ", ".join(f"{a} / {b}" for a, b in result.case_pairs))
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog=_CLI, description=(__doc__ or "").split("\n", 1)[0])
    sub = parser.add_subparsers(dest="verb", required=True)
    p_status = sub.add_parser("status", help="version, raft snapshot coverage, broken classes")
    p_status.add_argument("--url", default=None)
    p_status.add_argument("--json", action="store_true")
    p_snap = sub.add_parser("snapshot", help="take a raft snapshot with a container's own image")
    p_snap.add_argument("--container", required=True)
    p_snap.add_argument("--runtime", default=None)
    p_prep = sub.add_parser("prepare", help=(
        "before running VCO's compose yourself: take the pre-move snapshot when the "
        "recreate would move Weaviate data onto a release with the orphan drop"))
    p_prep.add_argument("--root", default=str(Path(__file__).resolve().parent.parent))
    p_prep.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.verb == "status":
        return _status(args.url or _weaviate_url(), args.json)
    if args.verb == "prepare":
        verdict = prepare_for_compose(Path(args.root),
                                      log=lambda m: print(m, file=sys.stderr))
        if args.json:
            reply: dict = {"ok": verdict.ok, "status": verdict.status, "message": verdict.reason}
            if not verdict.ok:
                reply["error"] = CID_UPGRADE_REFUSED
            print(json.dumps(reply, sort_keys=True))
        else:
            print(f"{verdict.status}: {verdict.reason}")
        return 0 if verdict.ok else 1
    runtime = args.runtime or _runtime()
    if not runtime:
        print("no container runtime found (pass --runtime podman|docker)", file=sys.stderr)
        return 2
    verdict = snapshot_with_container_image(args.container, runtime=runtime,
                                            run=subprocess.run, log=print)
    for line in verdict.lines:
        print(line)
    if not verdict.ok:
        print(f"snapshot refused: {verdict.reason}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
