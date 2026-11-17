# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The protected move of VCO's own Weaviate container onto the pinned image
(v0.2.107, owner ruling "recreate it to get latest").

From this release VCO supports only the Weaviate its compose pins, so an
update moves every Weaviate container VCO's own compose created onto it. The
data crosses versions WITH its vectors — nothing is re-chunked or re-embedded
— and Weaviate does not downgrade, so the move is made reversible first:

1. **Count** the objects of EVERY class on the running old version
   (:func:`class_count`: ``None`` when a count cannot be read — an unreadable
   count refuses the move; it never compares as "equal").
2. **Stop** the old container, then let the one recreate guard clear it
   (:func:`vco_lib.data_identity.guard_recreate` through
   ``service_lifecycle.up_services(guard_only=True)``): the data mount compose
   would give the new container is the live one, and a raft snapshot taken by
   the OLD image covers the whole schema log (``weaviate_raft_guard``), or the
   move is refused.
3. **Copy the data** to a new backup volume — after a free-space check made
   inside the engine (so it is right under Docker Desktop's VM too). When the
   copy does not fit, nothing is moved: the old container is started again
   and the ledger entry names both numbers.
4. **Recreate** from VCO's compose (the pinned image, the memory limit).
5. **Verify**: the new container runs the pinned image on the SAME mount,
   every class has its index, and every class holds exactly the count it had.
6. **Roll back** on any failure: the new container is removed, the volume is
   restored from the copy, and the OLD image is started on it again from the
   same compose with a one-line image override. The failed attempt is
   recorded, so the next update does not try again by itself (no auto-heal
   loop) until the user asks (``retry``).

This is the ONLY path that changes the version a Weaviate container runs:
every other recreate (install.py step 5, the session hooks' zombie recovery
and missing-container create through ``service_lifecycle up``, the launcher's
Services actions, the port move, the launcher's volume migration) asks
:func:`vco_lib.weaviate_raft_guard.upgrade_check`, which refuses a recreate
that would change a live container's version unless this module's guard call
asks (``protected_move=True``). A zombie whose recreate is refused that way is
started by name instead (``service_lifecycle.up_services``).

Only a container that is VCO's own reaches this module
(:func:`vco_lib.service_reconcile.reclassify_own` decides that); a container
VCO did not create is never recreated — it only gets the memory limit in
place (:func:`vco_lib.weaviate_memory.ensure_foreign_limit`).

``VCO_CONTAINER_UPGRADE=off`` keeps the old container (the update then leaves
it exactly as it is); ``VCO_CONTAINER_UPGRADE=dry-run`` prints what would be
done and does nothing.

Every engine interaction goes through :class:`ContainerEngine` (argv through
the injectable ``run``, HTTP through ``http``); the tests drive the whole flow
against a fake engine and never touch a container runtime.

CLI::

    python -m vco_lib.container_upgrade plan  [--root PATH]   # the dry-run line
    python -m vco_lib.container_upgrade status
    python -m vco_lib.container_upgrade retry --service weaviate
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from vco_lib.deferral_report import DeferralEntry

__all__ = [
    "CID_BACKUP_KEPT",
    "CID_REFUSED",
    "CID_ROLLED_BACK",
    "ContainerEngine",
    "MoveOutcome",
    "class_count",
    "class_counts",
    "image_move",
    "kept_backup_entries",
    "protect_weaviate_move",
    "version_move_refused_entry",
]

CID_REFUSED = "container_upgrade_refused"
CID_ROLLED_BACK = "container_upgrade_rolled_back"
CID_BACKUP_KEPT = "container_upgrade_backup_kept"

#: ``on`` (default) | ``off`` (keep the old container) | ``dry-run``.
MODE_ENV = "VCO_CONTAINER_UPGRADE"
#: Seconds to wait for the recreated Weaviate to answer ``/v1/.well-known/ready``.
READY_TIMEOUT_ENV = "VCO_CONTAINER_UPGRADE_READY_TIMEOUT_S"
DEFAULT_READY_TIMEOUT_S = 3600.0
#: The copy and the restore: no machine sets this ceiling (slow disks exist).
COPY_TIMEOUT_S = 24 * 3600.0
#: Free space required beyond the data's own size (file-system overhead).
HEADROOM = 1.10

NOT_NEEDED = "not_needed"
DRY_RUN = "dry_run"
OPTED_OUT = "opted_out"
REFUSED = "refused"
MOVED = "moved"
ROLLED_BACK = "rolled_back"
ROLLBACK_FAILED = "rollback_failed"

_CLI = "python -m vco_lib.container_upgrade"
_DATA_DEST = "/var/lib/weaviate"

RunFn = Callable[..., "subprocess.CompletedProcess[str]"]
HttpFn = Callable[[str, str, Optional[dict], float], "tuple[int, bytes]"]
LogFn = Callable[[str], None]


# ─── counts (the contract of the one count helper) ──────────────────────


def _default_http(method: str, url: str, body: Optional[dict], timeout: float) -> tuple[int, bytes]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"content-type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — a local URL
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read() or b""


def class_count(url: str, cls: str, *, http: Optional[HttpFn] = None) -> Optional[int]:
    """The number of objects in class *cls*, or ``None`` when it cannot be
    read: a transport error, a non-200 answer, an ``errors`` body, a missing
    or null field, a value that is not a non-negative integer, or a class
    name that is not ``[A-Za-z0-9_]+``. ``None`` is never "zero" and never
    "equal".

    THE count helper, ``weaviate_helpers.http_count_objects`` (one query, one
    reading of the reply, one class-name check for every VCO count); this
    module only lends it its own transport (*http*, the tests' fake engine
    seam) through the helper's ``request`` parameter."""
    from vco_lib.weaviate_helpers import http_count_objects  # noqa: PLC0415

    send = http or _default_http

    def request(method: str, target: str, *, body: Optional[dict] = None,
                timeout: float = 120.0) -> tuple[int, bytes]:
        return send(method, target, body, timeout)

    return http_count_objects(cls, url, timeout=120.0, request=request)


def schema_classes(url: str, *, http: Optional[HttpFn] = None) -> Optional[list[str]]:
    """Every class name in the schema, or ``None`` when it cannot be read."""
    try:
        status, raw = (http or _default_http)("GET", f"{url.rstrip('/')}/v1/schema", None, 60.0)
        payload = json.loads(raw.decode("utf-8", "replace")) if status == 200 else None
    except Exception:  # noqa: BLE001
        return None
    classes = payload.get("classes") if isinstance(payload, dict) else None
    if not isinstance(classes, list):
        return None
    names: list[str] = []
    for item in classes:
        name = item.get("class") if isinstance(item, dict) else None
        if not isinstance(name, str) or not name:
            return None
        names.append(name)
    return sorted(names)


def class_counts(url: str, *, http: Optional[HttpFn] = None
                 ) -> tuple[Optional[dict[str, int]], str]:
    """``(class → count for EVERY class, "")``, or ``(None, why)`` when the
    schema or any single count cannot be read."""
    names = schema_classes(url, http=http)
    if names is None:
        return None, "the class list (/v1/schema) could not be read"
    counts: dict[str, int] = {}
    for name in names:
        count = class_count(url, name, http=http)
        if count is None:
            return None, f"the object count of class {name} could not be read"
        counts[name] = count
    return counts, ""


def count_mismatches(before: Mapping[str, int], after: Optional[Mapping[str, int]]) -> list[str]:
    """Human lines for every class that LOST objects or disappeared — the
    failures of a move. An unreadable *after* is one line, never an empty
    list. More objects than before (or a new class) is not a loss: the
    counts are read while sessions can still write, before the container is
    stopped (audit C-4) — :func:`count_gains` reports those."""
    if after is None:
        return ["the counts after the move could not be read"]
    lines = []
    for name in sorted(before):
        b, a = before[name], after.get(name)
        if a is None or a < b:
            lines.append(f"{name}: {b} before, {a if a is not None else 'absent'} after")
    return lines


def count_gains(before: Mapping[str, int], after: Optional[Mapping[str, int]]) -> list[str]:
    """Human lines for every class that holds MORE objects than before, or
    that appeared — written between the count and the stop; reported in the
    success line, never a failure."""
    if after is None:
        return []
    lines = []
    for name in sorted(after):
        b, a = before.get(name), after[name]
        if b is None or a > b:
            lines.append(f"{name}: {b if b is not None else 'absent'} before, {a} after")
    return lines


# ─── what moves ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ImageMove:
    live: str          # the image reference the container runs
    pinned: str        # the image reference VCO's compose pins
    direction: str     # "upgrade" | "downgrade"


def image_move(live: str, pinned: Optional[str]) -> Optional[ImageMove]:
    """The version move a recreate from VCO's compose would make, or ``None``
    when there is none to judge: same version, another repository (the
    user's own build or mirror), or a tag without a version on either side.
    Parsing is :mod:`vco_lib.service_reconcile`'s (one home)."""
    from vco_lib import service_reconcile as _sr  # noqa: PLC0415 — it imports this module

    if not live or not pinned:
        return None
    live_repo, live_tag = _sr._image_repo_and_tag(live)
    pin_repo, pin_tag = _sr._image_repo_and_tag(pinned)
    if live_repo != pin_repo:
        return None
    live_v, pin_v = _sr._tag_version(live_tag), _sr._tag_version(pin_tag)
    if live_v is None or pin_v is None or live_v == pin_v:
        return None
    return ImageMove(live, pinned, "upgrade" if live_v < pin_v else "downgrade")


def upgrade_mode(environ: Optional[Mapping[str, str]] = None) -> str:
    """``on`` / ``off`` / ``dry-run`` from :data:`MODE_ENV` (anything else is
    ``on`` — an unknown word must not silently hold an update back)."""
    value = ((os.environ if environ is None else environ).get(MODE_ENV) or "").strip().lower()
    if value in ("off", "0", "false", "no"):
        return "off"
    if value in ("dry-run", "dry_run", "dryrun", "plan"):
        return "dry-run"
    return "on"


# ─── the failed-attempt record (no auto-heal loop) ──────────────────────


def state_path() -> Path:
    from vco_lib.paths import vct_root_dir  # noqa: PLC0415

    return vct_root_dir() / "state" / "container-upgrade.json"


def load_state(path: Optional[Path] = None) -> dict:
    try:
        data = json.loads((path or state_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(state: Mapping[str, Any], path: Optional[Path] = None) -> None:
    from vco_lib.atomic import atomic_write_text  # noqa: PLC0415

    target = path or state_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(target, json.dumps(dict(state), indent=2, sort_keys=True) + "\n")


def previous_failure(service: str, move: ImageMove, path: Optional[Path] = None) -> Optional[dict]:
    rec = load_state(path).get(service)
    if isinstance(rec, dict) and rec.get("from") == move.live and rec.get("to") == move.pinned:
        return rec
    return None


def write_image_override(image: str) -> Path:
    """The one-line compose file naming the image that ran on the data
    before the move — appended LAST to the ``-f`` chain by the rollback, and
    by ``hand-to-vco``'s take-over, which keeps the version the container
    runs so that only the protected move changes it. Kept outside the
    checkout, under the VCT state directory."""
    from vco_lib.atomic import atomic_write_text  # noqa: PLC0415

    path = state_path().parent / "container-upgrade-rollback-weaviate.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, "# Written by vco_lib.container_upgrade: the image that ran on "
                            "this data before the move.\n"
                            f"services:\n  weaviate:\n    image: {json.dumps(image)}\n")
    return path


# ─── the engine ─────────────────────────────────────────────────────────


class ContainerEngine:
    """Everything the move asks of the container runtime, compose and
    Weaviate's HTTP API. The tests replace this class; the argv each method
    builds is pinned by its own tests through the ``run`` seam."""

    def __init__(self, runtime: str, root: Path, *, run: Optional[RunFn] = None,
                 http: Optional[HttpFn] = None, db_path: Optional[Path] = None,
                 log: Optional[LogFn] = None,
                 sleep: Callable[[float], None] = time.sleep,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        if run is None:
            from vco_lib import tool_search_dirs as _tsd  # noqa: PLC0415

            run = _tsd.run
        self.runtime, self.root = runtime, Path(root)
        self.run, self.http, self.db_path = run, http or _default_http, db_path
        self.log: LogFn = log or (lambda _m: None)
        self.sleep, self.monotonic = sleep, monotonic

    # -- plain runtime verbs --------------------------------------------
    def _rt(self, args: Sequence[str], timeout: float = 180.0
            ) -> "Optional[subprocess.CompletedProcess[str]]":
        try:
            return self.run([self.runtime, *args], capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.SubprocessError):
            return None

    def _ok(self, args: Sequence[str], timeout: float = 180.0) -> bool:
        res = self._rt(args, timeout)
        return res is not None and res.returncode == 0

    def inspect(self, name: str) -> Any:
        """The container's :class:`vco_lib.service_detection.ContainerInfo`,
        or ``None`` when it does not exist or cannot be inspected."""
        from vco_lib import service_detection as _det  # noqa: PLC0415

        found = _det.list_containers_or_none(self.runtime, run=self.run, names={name})
        return next((c for c in found or [] if c.name == name), None)

    def stop(self, name: str) -> bool:
        return self._ok(["stop", "-t", "120", name], 300)

    def start(self, name: str) -> bool:
        return self._ok(["start", name], 300)

    def remove_container(self, name: str) -> bool:
        """Remove the CONTAINER only — never ``-v``, never a volume."""
        return self._ok(["rm", "--force", name], 120)

    def create_volume(self, name: str) -> bool:
        return self._ok(["volume", "create", name], 60)

    def remove_volume(self, name: str) -> bool:
        return self._ok(["volume", "rm", name], 120)

    def volume_exists(self, name: str) -> Optional[bool]:
        """``True`` / ``False`` (the engine positively says "no such
        volume") / ``None`` (it could not be asked)."""
        res = self._rt(["volume", "inspect", name], 60)
        if res is None:
            return None
        if res.returncode == 0:
            return True
        return False if "no such volume" in (res.stderr or "").lower() else None

    # -- one-off helper containers (the OLD image's own shell) ----------
    def _helper(self, image: str, mounts: Sequence[str], script: str, timeout: float
                ) -> Optional[list[str]]:
        argv = ["run", "--rm", "--network", "none", "--user", "0:0", "--entrypoint", "/bin/sh"]
        for mount in mounts:
            argv += ["-v", mount]
        argv += [image, "-c", script]
        res = self._rt(argv, timeout)
        if res is None or res.returncode != 0:
            return None
        return [line.strip() for line in (res.stdout or "").splitlines() if line.strip()]

    @staticmethod
    def _ints(lines: Optional[list[str]], n: int) -> Optional[tuple[int, ...]]:
        if lines is None or len(lines) < n or not all(x.isdecimal() for x in lines[-n:]):
            return None
        return tuple(int(x) for x in lines[-n:])

    def measure_kb(self, source: str, backup: str, image: str) -> Optional[tuple[int, int]]:
        """``(KiB the data uses, KiB free where *backup* lives)``, measured
        inside the engine; ``None`` when either cannot be read."""
        got = self._ints(self._helper(
            image, [f"{source}:/vco-from:ro", f"{backup}:/vco-to"],
            "du -sk /vco-from | cut -f1 && df -Pk /vco-to | tail -n 1 | awk '{print $4}'",
            3600), 2)
        return (got[0], got[1]) if got else None

    def copy_data(self, source: str, backup: str, image: str) -> bool:
        """Copy the data into the (empty) backup volume; ``True`` only when
        both sides then hold the same number of files."""
        got = self._ints(self._helper(
            image, [f"{source}:/vco-from:ro", f"{backup}:/vco-to"],
            "cp -a /vco-from/. /vco-to/ && find /vco-from -type f | wc -l "
            "&& find /vco-to -type f | wc -l", COPY_TIMEOUT_S), 2)
        return bool(got) and got[0] == got[1]

    def restore_data(self, source: str, backup: str, image: str) -> bool:
        """Put the copy back: empty the data location, copy the backup in,
        and confirm both sides hold the same number of files."""
        got = self._ints(self._helper(
            image, [f"{backup}:/vco-from:ro", f"{source}:/vco-to"],
            "find /vco-to -mindepth 1 -delete && cp -a /vco-from/. /vco-to/ "
            "&& find /vco-from -type f | wc -l && find /vco-to -type f | wc -l",
            COPY_TIMEOUT_S), 2)
        return bool(got) and got[0] == got[1]

    # -- compose --------------------------------------------------------
    def _compose_argv(self) -> Optional[list[str]]:
        from vco_lib import containers as _containers  # noqa: PLC0415

        found = _containers.compose_command(self.runtime)
        return list(found[0]) if found else None

    def guard(self) -> tuple[bool, str, list]:
        """The one recreate guard for the Weaviate service, without the
        compose call: data mount proven, raft snapshot taken by the old image
        (or refused). ``(cleared, why not, ledger entries)``."""
        from vco_lib import service_lifecycle as _sl  # noqa: PLC0415

        compose = self._compose_argv()
        if compose is None:
            return False, f"{self.runtime} has no compose available", []
        res = _sl.up_services(["weaviate"], compose_dir=self.root / "infrastructure",
                              compose_argv=compose, runtime=self.runtime, guard_only=True,
                              protected_move=True, db_path=self.db_path, run=self.run,
                              out=self.log)
        if "weaviate" in res.cleared:
            return True, "", list(res.entries)
        return False, "the recreate guard refused it (see the lines above)", list(res.entries)

    def compose_up(self, image_override: Optional[str] = None) -> tuple[bool, str]:
        """``compose up -d --force-recreate --no-deps weaviate`` under the
        installer's project. *image_override* (the rollback) appends a one-line
        compose file naming the OLD image.

        MUST MATCH the compose tail of ``service_lifecycle.up_services`` (the
        ``-f`` chain from ``data_identity.compose_files``, the installer's
        ``-p``, ``compose_up_with_recovery``): that function runs its guard
        again when asked to compose, which would take a second raft snapshot."""
        from vco_lib import compose_provider as _cp  # noqa: PLC0415
        from vco_lib import compose_recovery as _cr  # noqa: PLC0415
        from vco_lib import containers as _containers  # noqa: PLC0415
        from vco_lib import data_identity as _di  # noqa: PLC0415
        from vco_lib import service_lifecycle as _sl  # noqa: PLC0415

        compose = self._compose_argv()
        if compose is None:
            return False, f"{self.runtime} has no compose available"
        infra = self.root / "infrastructure"
        files = list(_di.compose_files(infra))
        project = _containers.compose_project_of(files[0])
        if image_override:
            files.append(self.write_image_override(image_override))
        args, _dropped = _sl.compose_up_args(["weaviate"], force_recreate=True)
        chain: list[str] = []
        for path in files:
            chain += ["-f", str(path)]
        argv = [*compose, *chain, *(["-p", project] if project else []), *args]
        up = _cr.compose_up_with_recovery(
            argv, cwd=str(infra), timeout=900, runtime=self.runtime,
            provider=_cp.detect(self.runtime, argv=compose, run=self.run),
            run=self.run, compose_run=self.run, log=self.log)
        if up.ok:
            return True, f"cd {infra} && {shlex.join(argv)}"
        tail = (up.first_stderr or up.stderr or "").strip().splitlines()[-1:]
        return False, (tail[0][:300] if tail else "compose up failed") \
            + f" (cd {infra} && {shlex.join(argv)})"

    def write_image_override(self, image: str) -> Path:
        return write_image_override(image)

    # -- Weaviate over HTTP ---------------------------------------------
    def wait_ready(self, url: str, timeout_s: float) -> bool:
        deadline = self.monotonic() + max(0.0, timeout_s)
        while True:
            try:
                status, _body = self.http("GET", f"{url.rstrip('/')}/v1/.well-known/ready", None, 10.0)
            except Exception:  # noqa: BLE001 — not up yet
                status = 0
            if status == 200:
                return True
            if self.monotonic() >= deadline:
                return False
            self.sleep(5.0)

    def counts(self, url: str) -> tuple[Optional[dict[str, int]], str]:
        return class_counts(url, http=self.http)

    def indexes_missing(self, url: str, classes: Sequence[str]) -> Optional[list[str]]:
        """Classes that are in the schema without their index (the 1.37
        orphan drop's footprint); ``None`` when one could not be checked."""
        from vco_lib import weaviate_raft_guard as _rg  # noqa: PLC0415

        missing: list[str] = []
        for cls in classes:
            verdict = _rg.index_missing(
                url, cls, http=lambda u, t: self.http("GET", u, None, t))
            if verdict is None:
                return None
            if verdict:
                missing.append(cls)
        return missing


# ─── the move ───────────────────────────────────────────────────────────


@dataclass
class MoveOutcome:
    service: str
    status: str = NOT_NEEDED
    reason: str = ""
    lines: list[str] = field(default_factory=list)
    entries: list = field(default_factory=list)
    backup: str = ""
    #: classes that held 0 objects before AND after a verified move (reported;
    #: nothing is dropped here).
    empty_classes: list[str] = field(default_factory=list)
    #: live progress (a multi-gigabyte copy must not look like a hang); every
    #: line is also kept in ``lines``.
    log: Optional[LogFn] = field(default=None, repr=False, compare=False)

    def say(self, line: str) -> None:
        self.lines.append(line)
        if self.log is not None:
            self.log(line)

    @property
    def blocks_recreate(self) -> bool:
        """The update's own compose step must leave this container alone: the
        move was held back, refused or rolled back, and an unprotected
        recreate would make exactly the move that was not made."""
        return self.status in (DRY_RUN, OPTED_OUT, REFUSED, ROLLED_BACK, ROLLBACK_FAILED)


def ready_timeout_s(environ: Optional[Mapping[str, str]] = None) -> float:
    raw = ((os.environ if environ is None else environ).get(READY_TIMEOUT_ENV) or "").strip()
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_READY_TIMEOUT_S
    return value if value > 0 else DEFAULT_READY_TIMEOUT_S


def plan_line(name: str, move: ImageMove, mount: Optional[Mapping[str, str]]) -> str:
    where = f"{mount.get('kind')} {mount.get('source')}" if mount else "its data"
    return (f"  [weaviate] {name}: {move.live} -> {move.pinned}. Steps: count every class; stop "
            f"it; raft snapshot with the old image; copy {where} to a new backup volume (after "
            "a free-space check); recreate from VCO's compose; verify image, mount, indexes "
            "and every count; on a mismatch restore the copy and start the old image again.")


def _gib(kb: int) -> str:
    return f"{kb / (1024 * 1024):.1f} GiB"


def _refused_entry(name: str, move: ImageMove, reason: str, command: str) -> DeferralEntry:
    return DeferralEntry(
        condition_id=CID_REFUSED,
        title="Weaviate was not moved to the version VCO supports",
        detected=(f"Container {name} runs {move.live}; VCO supports {move.pinned}. The move was "
                  f"not made: {reason}. Nothing was removed; the container runs as before."),
        why_deferred=("VCO moves Weaviate's data to a newer version only with a verified copy "
                      "to go back to. Until the move is made, VCO's KG and code-graph features "
                      "run against a Weaviate version this release does not support."),
        command_to_apply=command,
        severity="warning",
        dismiss_fields={"service": "weaviate", "container": name},
    )


def version_move_refused_entry(name: str, move: Optional[ImageMove], reason: str
                               ) -> DeferralEntry:
    """The ledger row of a recreate that the one recreate guard refused
    because it would change the Weaviate version outside this module
    (``data_identity.REFUSE_VERSION``) — install.py step 5 records it for a
    container the protected move does not take (not provably VCO's own)."""
    if move is None:  # never None from the guard; kept total
        move = ImageMove("?", "?", "upgrade")
    return _refused_entry(
        name or "vco_weaviate", move, reason,
        "# See what VCO's protected move would do with this container:\n"
        f"{_CLI} plan\n"
        "# A container another compose created is handed to VCO (and moved, protected) with:\n"
        "python -m vco_lib.service_endpoints hand-to-vco --service weaviate")


def _rolled_back_entry(name: str, move: ImageMove, reason: str, backup: str, restored: bool,
                       manual: str) -> DeferralEntry:
    state = (f"The data was restored from the copy and {move.live} runs on it again."
             if restored else
             "The automatic restore did NOT finish — Weaviate may be down. The copy is intact.")
    return DeferralEntry(
        condition_id=CID_ROLLED_BACK,
        title="The move of Weaviate to the supported version failed and was undone",
        detected=(f"Container {name}: {move.live} -> {move.pinned} failed its check: {reason}. "
                  f"{state} Backup volume: {backup}."),
        why_deferred=("A failed move is not repeated by itself: the next update leaves this "
                      "container alone until you ask for another attempt."),
        command_to_apply=(
            (manual + "\n" if manual else "")
            + "# Try the move again at the next update (after reading the reason above):\n"
            f"{_CLI} retry --service weaviate\npython install.py --update"),
        severity="critical" if not restored else "warning",
        dismiss_fields={"service": "weaviate", "container": name},
    )


def _backup_kept_entry(name: str, move: ImageMove, backup: str, runtime: str,
                       empty: Sequence[str]) -> DeferralEntry:
    extra = (f" {len(empty)} classes held no objects before and after the move "
             f"({', '.join(list(empty)[:8])}{', …' if len(empty) > 8 else ''}); none was dropped."
             if empty else "")
    return DeferralEntry(
        condition_id=CID_BACKUP_KEPT,
        title="Weaviate now runs the supported version; the pre-move copy is kept",
        detected=(f"Container {name} moved {move.live} -> {move.pinned}; no class lost an "
                  f"object. The data as it was before the move is in volume "
                  f"{backup}.{extra}"),
        why_deferred="Recorded for your information; VCO never deletes that copy itself.",
        command_to_apply=("# Once you are satisfied with the new version, free the space:\n"
                          f"{runtime} volume rm {backup}"),
        severity="info",
    )


def protect_weaviate_move(
    *,
    root: Path,
    runtime: str,
    row: Any,
    container: Any,
    engine: Optional[ContainerEngine] = None,
    environ: Optional[Mapping[str, str]] = None,
    state_file: Optional[Path] = None,
    now: Optional[Callable[[], float]] = None,
    log: Optional[LogFn] = None,
) -> MoveOutcome:
    """Move VCO's own Weaviate *container* (its ``ContainerInfo``; *row* is
    its ``vco_managed`` row) onto the image VCO's compose pins — or decide,
    with the reason, that nothing is moved. See the module docstring for the
    order; every refusal leaves the old container as it was found."""
    from vco_lib import data_identity as _di  # noqa: PLC0415
    from vco_lib import service_adoption as _sa  # noqa: PLC0415
    from vco_lib import service_endpoints as _se  # noqa: PLC0415

    out = MoveOutcome("weaviate", log=log)
    name = container.name
    move = image_move(container.image, _sa.pinned_image("weaviate", Path(root)))
    if move is None:
        return out
    mount_obj = container.mount_at(_DATA_DEST)
    mount = mount_obj.to_row() if mount_obj is not None else None
    if held_back(out, name, move, mount, environ=environ, state_file=state_file):
        return out
    if mount is None:
        return _refuse(out, name, move, f"{name} mounts nothing at {_DATA_DEST}",
                       "python install.py --update")
    eng = engine or ContainerEngine(runtime, Path(root))
    url = _se.render_url("weaviate", row)
    say = out.say
    say(f"  [weaviate] moving {name} from {move.live} to {move.pinned} (VCO's own container)")

    if not container.running:
        # Counts need the old version answering: start it BY NAME first.
        if not eng.start(name) or not eng.wait_ready(url, ready_timeout_s(environ)):
            return _refuse(out, name, move, (
                f"{name} is stopped and did not come up, so its classes could not be counted"),
                f"# Start it, then update again:\n{runtime} start {name}\npython install.py --update")
    before, why = eng.counts(url)
    if before is None:
        return _refuse(out, name, move, f"{why} on the running {move.live}",
                       "# Start Weaviate, then:\npython install.py --update")
    say(f"  [weaviate] {len(before)} classes, {sum(before.values())} objects counted")
    if not eng.stop(name):
        return _refuse(out, name, move, f"{name} could not be stopped", "python install.py --update")

    def back_to_old(reason: str, command: str = "python install.py --update",
                    entries: Sequence[Any] = ()) -> MoveOutcome:
        started = eng.start(name)
        out.entries.extend(entries)
        return _refuse(out, name, move, reason + ("" if started else
                       f"; {name} could NOT be started again — start it by hand: "
                       f"{runtime} start {name}"), command)

    cleared, why, guard_entries = eng.guard()
    if not cleared:
        return back_to_old(why, entries=guard_entries)
    stamp = time.strftime("%Y%m%d%H%M%S", time.gmtime((now or time.time)()))
    backup = f"vco-weaviate-backup-{stamp}"
    if not eng.create_volume(backup):
        return back_to_old(f"the backup volume {backup} could not be created")
    source = mount["source"]
    measured = eng.measure_kb(source, backup, move.live)
    if measured is None:
        eng.remove_volume(backup)  # VCO's own, created empty a moment ago
        return back_to_old("the size of the data and the free space for its copy could not be "
                           "measured")
    used_kb, free_kb = measured
    need_kb = int(used_kb * HEADROOM)
    if free_kb < need_kb:
        eng.remove_volume(backup)
        return back_to_old(
            f"the copy does not fit: the data is {_gib(used_kb)} (needs {_gib(need_kb)} with "
            f"headroom) and the container engine's volume storage has {_gib(free_kb)} free",
            f"# Free at least {_gib(need_kb - free_kb)} where {runtime} keeps its volumes, then:\n"
            "python install.py --update")
    say(f"  [weaviate] copying {_gib(used_kb)} to volume {backup} ({_gib(free_kb)} free)")
    if not eng.copy_data(source, backup, move.live):
        eng.remove_volume(backup)  # an incomplete copy of data that is still in place
        return back_to_old("the copy of the data did not complete (file counts differ or the "
                           "copy failed)")
    out.backup = backup

    # ── from here the old container is replaced; every failure rolls back ──
    problem, gains = "", []
    if not eng.remove_container(name):
        return back_to_old(f"the old container {name} could not be removed (the copy is in "
                           f"volume {backup}; remove it with `{runtime} volume rm {backup}`)")
    ok, detail = eng.compose_up(None)
    if not ok:
        problem = f"compose could not create the new container: {detail}"
    elif not eng.wait_ready(url, ready_timeout_s(environ)):
        problem = f"{move.pinned} did not become ready at {url}"
    else:
        problem, gains = _verify(eng, name, url, move, mount, before, _di)
    if not problem:
        after_empty = sorted(c for c, n in before.items() if n == 0)
        out.status, out.empty_classes = MOVED, after_empty
        _record_backup(out, name, move, backup, runtime, stamp, state_file)
        say(f"  [weaviate] verified: {move.pinned}, same data mount, every index present, "
            f"no class lost an object (of {len(before)} counted)"
            + (f"; written during the move: {'; '.join(gains[:10])}" if gains else ""))
        say(f"  [weaviate] the pre-move copy is kept in volume {backup}")
        out.entries.append(_backup_kept_entry(name, move, backup, runtime, after_empty))
        return out

    say(f"  [weaviate] the move FAILED its check — {problem}. Rolling back.")
    restored, manual = _rollback(eng, name, url, move, source, backup, before, runtime, root,
                                 ready_timeout_s(environ))
    out.status = ROLLED_BACK if restored else ROLLBACK_FAILED
    out.reason = problem
    say(f"  [weaviate] {'restored: ' + move.live + ' runs on the data as it was' if restored else 'the automatic restore did NOT finish — see UPDATE_DEFERRED.md'}")
    state = load_state(state_file)
    state["weaviate"] = {"from": move.live, "to": move.pinned, "reason": problem,
                         "backup": backup, "restored": restored, "at": stamp}
    try:
        save_state(state, state_file)
    except OSError as exc:
        say(f"  [weaviate] the failed attempt could not be recorded ({exc})")
    out.entries.append(_rolled_back_entry(name, move, problem, backup, restored, manual))
    return out


def held_back(out: MoveOutcome, name: str, move: ImageMove, mount: Optional[Mapping[str, str]],
              *, environ: Optional[Mapping[str, str]] = None,
              state_file: Optional[Path] = None) -> bool:
    """The decisions made before anything is asked of the engine: a
    downgrade (Weaviate does not downgrade), ``VCO_CONTAINER_UPGRADE=dry-run``
    / ``off``, and an earlier failed attempt (not repeated by itself). Fills
    *out* and returns ``True`` when the move is held back. Also asked by
    ``hand-to-vco`` BEFORE it takes a container over, so a hand-over never
    leaves a container VCO's own on a version it then cannot move."""
    if move.direction == "downgrade":
        out.status, out.reason = REFUSED, (
            f"{name} runs {move.live}, NEWER than the {move.pinned} VCO pins; Weaviate does "
            "not downgrade, so VCO leaves it as it is")
        out.say(f"  [weaviate] {out.reason}.")
        return True
    mode = upgrade_mode(environ)
    if mode == "dry-run":
        out.status, out.reason = DRY_RUN, f"{MODE_ENV}=dry-run"
        out.say(f"  [weaviate] {MODE_ENV}=dry-run — nothing is changed. It would do this:")
        out.say(plan_line(name, move, mount))
        return True
    if mode == "off":
        out.status, out.reason = OPTED_OUT, f"{MODE_ENV}=off"
        out.say(f"  [weaviate] {MODE_ENV}=off — {name} stays on {move.live} "
                f"(VCO supports {move.pinned}); nothing is recreated.")
        return True
    earlier = previous_failure("weaviate", move, state_file)
    if earlier is not None:
        _refuse(out, name, move, (
            f"an earlier attempt failed ({earlier.get('reason') or 'no reason recorded'}) and a "
            "failed move is not repeated by itself"),
            f"# Ask for another attempt:\n{_CLI} retry --service weaviate\n"
            "python install.py --update")
        return True
    return False


def _record_backup(out: MoveOutcome, name: str, move: ImageMove, backup: str, runtime: str,
                   stamp: str, state_file: Optional[Path]) -> None:
    """Keep the name of the pre-move copy in ``container-upgrade.json``: the
    ledger entry is re-emitted from it by every later update while the
    volume exists (:func:`kept_backup_entries`, audit C-2)."""
    state = load_state(state_file)
    kept = [b for b in state.get("backups") or [] if isinstance(b, dict)]
    kept.append({"service": "weaviate", "volume": backup, "container": name, "from": move.live,
                 "to": move.pinned, "runtime": runtime, "at": stamp})
    state["backups"] = kept
    try:
        save_state(state, state_file)
    except OSError as exc:
        out.say(f"  [weaviate] the backup volume {backup} could not be recorded ({exc}); "
                "UPDATE_DEFERRED.md names it this once")


def kept_backup_entries(engine: ContainerEngine, *, skip: Sequence[str] = (),
                        state_file: Optional[Path] = None) -> list[DeferralEntry]:
    """The :data:`CID_BACKUP_KEPT` entry for every recorded pre-move copy
    that still exists (re-emitted by every update, so the one record of a
    full copy of Weaviate's data is not dropped while the copy takes the
    space; VCO never deletes the volume itself). A volume the engine
    positively reports gone is forgotten; one that cannot be checked is kept
    and still reported. *skip*: volumes this run already reported."""
    state = load_state(state_file)
    recorded = [b for b in state.get("backups") or [] if isinstance(b, dict) and b.get("volume")]
    if not recorded:
        return []
    entries: list[DeferralEntry] = []
    keep: list[dict] = []
    for rec in recorded:
        volume = str(rec["volume"])
        if engine.volume_exists(volume) is False:
            continue  # the user removed it: nothing left to report
        keep.append(rec)
        if volume in skip:
            continue
        move = ImageMove(str(rec.get("from") or "?"), str(rec.get("to") or "?"), "upgrade")
        entries.append(_backup_kept_entry(str(rec.get("container") or "vco_weaviate"), move,
                                          volume, str(rec.get("runtime") or engine.runtime), ()))
    if len(keep) != len(recorded):
        state["backups"] = keep
        try:
            save_state(state, state_file)
        except OSError:
            pass  # re-checked at the next update; nothing is deleted on this path
    return entries


def _refuse(out: MoveOutcome, name: str, move: ImageMove, reason: str, command: str) -> MoveOutcome:
    out.status, out.reason = REFUSED, reason
    out.say(f"  [weaviate] not moved — {reason}. {name} stays on {move.live}.")
    out.entries.append(_refused_entry(name, move, reason, command))
    return out


def _verify(eng: ContainerEngine, name: str, url: str, move: ImageMove,
            mount: Mapping[str, str], before: Mapping[str, int], _di: Any
            ) -> tuple[str, list[str]]:
    """``("", gains)`` when the recreated container is the pinned image on
    the same data with every class intact (*gains*: :func:`count_gains`),
    else ``(the first thing that is not, [])``."""
    live = eng.inspect(name)
    if live is None:
        return f"the new container {name} could not be inspected", []
    if image_move(live.image, move.pinned) is not None:
        return f"the new container runs {live.image}, not {move.pinned}", []
    new_mount = live.mount_at(_DATA_DEST)
    if new_mount is None or _di.mount_key(new_mount.to_row()) != _di.mount_key(dict(mount)):
        got = f"{new_mount.kind} {new_mount.source}" if new_mount is not None else "nothing"
        return (f"the new container mounts {got} at {_DATA_DEST}, not "
                f"{mount.get('kind')} {mount.get('source')}"), []
    missing = eng.indexes_missing(url, sorted(before))
    if missing is None:
        return "whether every class still has its index could not be checked", []
    if missing:
        return "classes without their index: " + ", ".join(missing[:10]), []
    after, why = eng.counts(url)
    if after is None:
        return why, []
    bad = count_mismatches(before, after)
    if bad:
        return "object counts differ — " + "; ".join(bad[:10]), []
    return "", count_gains(before, after)


def _rollback(eng: ContainerEngine, name: str, url: str, move: ImageMove, source: str,
              backup: str, before: Mapping[str, int], runtime: str, root: Path,
              ready_s: float) -> tuple[bool, str]:
    """Remove the new container, restore the copy, start the OLD image on it.
    ``(the old version answers with every count as before, the manual command
    for when it does not)``."""
    infra = Path(root) / "infrastructure"
    manual = ("# Restore by hand (Weaviate stopped): empty the data location, copy the backup "
              "volume back,\n# then start the old image from VCO's compose:\n"
              f"{runtime} run --rm --user 0:0 --entrypoint /bin/sh -v {backup}:/vco-from:ro "
              f"-v {source}:/vco-to {move.live} -c "
              "'find /vco-to -mindepth 1 -delete && cp -a /vco-from/. /vco-to/'\n"
              f"cd {infra}  # then compose up weaviate with image {move.live}")
    eng.stop(name)
    eng.remove_container(name)  # the NEW container; its data is restored below
    if not eng.restore_data(source, backup, move.live):
        return False, manual
    ok, _detail = eng.compose_up(move.live)
    if not ok or not eng.wait_ready(url, ready_s):
        return False, manual
    after, _why = eng.counts(url)
    return (not count_mismatches(before, after)), manual


# ─── CLI ────────────────────────────────────────────────────────────────


def _cli_plan(root: Path) -> int:
    from vco_lib import containers as _containers  # noqa: PLC0415
    from vco_lib import service_adoption as _sa  # noqa: PLC0415
    from vco_lib import service_detection as _det  # noqa: PLC0415

    try:
        runtime = _containers.resolve(probe_compose=False).runtime
    except Exception:  # noqa: BLE001
        runtime = None
    name = _containers.canonical_name("weaviate")
    found = _det.list_containers_or_none(runtime, names={name}) if runtime else None
    container = next((c for c in found or [] if c.name == name), None)
    if container is None:
        print(f"weaviate: no container {name} found — nothing to move")
        return 0
    move = image_move(container.image, _sa.pinned_image("weaviate", root))
    if move is None or move.direction != "upgrade":
        print(f"weaviate: {name} runs {container.image} — nothing to move")
        return 0
    mount = container.mount_at(_DATA_DEST)
    print(plan_line(name, move, mount.to_row() if mount else None).strip())
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog=_CLI)
    sub = parser.add_subparsers(dest="verb", required=True)
    plan = sub.add_parser("plan", help="print what an update would do to VCO's own Weaviate")
    plan.add_argument("--root", default=None)
    sub.add_parser("status", help="print the recorded failed attempts")
    retry = sub.add_parser("retry", help="forget a failed attempt so the next update tries again")
    retry.add_argument("--service", required=True, choices=("weaviate",))
    args = parser.parse_args(argv)
    if args.verb == "plan":
        return _cli_plan(Path(args.root) if args.root else Path(__file__).resolve().parent.parent)
    state = load_state()
    if args.verb == "status":
        print(json.dumps(state, indent=2, sort_keys=True))
        return 0
    if args.service in state:
        rec = state.pop(args.service)
        save_state(state)
        print(f"{args.service}: the failed attempt ({rec.get('from')} -> {rec.get('to')}) is "
              "forgotten; the next `python install.py --update` tries the move again. The backup "
              f"volume {rec.get('backup')} is untouched.")
    else:
        print(f"{args.service}: no failed attempt is recorded")
    return 0


if __name__ == "__main__":  # pragma: no cover — CLI
    sys.exit(main())
