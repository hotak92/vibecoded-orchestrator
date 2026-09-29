# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""vco_lib.compose_recovery — typed compose failures, honest remedies, safe heals.

v0.2.100 (AD-3; review L1-F03/F04/F06/F07/F22). Before this module a failed
``compose up`` was read with bare substring tests (``"daemon" in stderr`` →
"container daemon not running", ``"bind" in stderr`` → "port in use"), so the
2026-09-29 update printed "daemon not running" for three failures that were
none of those: a vanished API socket file, a network labelled by a different
compose tool, and a storage-only leftover holding a container name. The one
recovery that existed (network label) lived in one caller only.

* :func:`classify` maps stderr to a :class:`ComposeFailure` with ANCHORED
  patterns (whole phrases, never a bare word), keeping the real line as
  evidence and a cause-specific remedy.
* :func:`heal` applies the ONE non-destructive repair per healable cause:

  - socket missing → :func:`vco_lib.compose_provider.heal_socket`;
  - network label mismatch → ``network rm`` ONLY when
    ``<runtime> ps -a --filter network=<n> -q`` is positively empty;
  - storage-only leftover → only for a canonical VCO container name, only
    after BOTH mountinfo views (host and ``podman unshare``) show nothing of
    it, then a NON-recursive ``rmdir`` of empty leftover directories under its
    ``merged/`` and ``podman rm --storage <id>``. A file, a symlink or a mount
    point under ``merged/`` refuses the heal: a recursive delete there could
    walk into a live bind mount's host data.

  Anything that cannot be positively proven safe is REFUSED with a ledger row
  id (never guessed at, never "cleaned up anyway").
* :func:`compose_up_with_recovery` runs ``compose up``, keeps the FIRST stderr,
  heals, and retries; ``--build`` is dropped ONLY on positive evidence that the
  compose rejects the flag (``BUILD_FLAG_UNSUPPORTED``) — a real build failure
  is surfaced with its reason, never masked by a silent retry.
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Sequence

from vco_lib import compose_provider as _cp
from vco_lib import containers as _c
from vco_lib import tool_search_dirs as _tsd
from vco_lib.compose_provider import HealResult
from vco_lib.deferral_report import DeferralEntry

__all__ = [
    "SOCKET_MISSING", "DAEMON_DOWN", "PORT_TAKEN",
    "NAME_CONFLICT_STORAGE_LEFTOVER", "NAME_CONFLICT_OTHER_PROVIDER",
    "NETWORK_LABEL_MISMATCH", "PROVIDER_MISMATCH", "BUILD_FAILED",
    "BUILD_FLAG_UNSUPPORTED", "UNKNOWN",
    "ComposeFailure", "HealResult", "UpResult",
    "classify", "refine", "heal", "compose_up_with_recovery", "deferral_entries",
    "heal_deferral_entry", "daemon_remedy",
    "CID_SOCKET_HEAL_FAILED", "CID_PROVIDER_MISMATCH",
    "CID_NETWORK_LABEL_ATTACHED", "CID_STORAGE_LEFTOVER_UNSAFE",
]

RunFn = Callable[..., "subprocess.CompletedProcess[str]"]
LogFn = Callable[[str], None]

SOCKET_MISSING = "socket_missing"
DAEMON_DOWN = "daemon_down"
PORT_TAKEN = "port_taken"
NAME_CONFLICT_STORAGE_LEFTOVER = "name_conflict_storage_leftover"
NAME_CONFLICT_OTHER_PROVIDER = "name_conflict_other_provider"
NETWORK_LABEL_MISMATCH = "network_label_mismatch"
PROVIDER_MISMATCH = "provider_mismatch"
BUILD_FAILED = "build_failed"
BUILD_FLAG_UNSUPPORTED = "build_flag_unsupported"
UNKNOWN = "unknown"

CID_SOCKET_HEAL_FAILED = "compose_socket_heal_failed"
CID_PROVIDER_MISMATCH = "compose_provider_mismatch"
CID_NETWORK_LABEL_ATTACHED = "compose_network_label_mismatch_attached"
CID_STORAGE_LEFTOVER_UNSAFE = "container_storage_leftover_unsafe"

#: Human label per cause (the renderer's "Cause:" line).
CAUSE_LABELS = {
    SOCKET_MISSING: "the container API socket is missing",
    DAEMON_DOWN: "the container daemon / machine is not running",
    PORT_TAKEN: "a host port is already in use",
    NAME_CONFLICT_STORAGE_LEFTOVER: "a storage-only leftover still owns the container name",
    NAME_CONFLICT_OTHER_PROVIDER: "the container name belongs to a container this compose did not create",
    NETWORK_LABEL_MISMATCH: "compose network created by a different compose tool (label mismatch)",
    PROVIDER_MISMATCH: "the container was created by a different compose tool",
    BUILD_FAILED: "the image build failed",
    BUILD_FLAG_UNSUPPORTED: "this compose does not accept `--build` on `up`",
    UNKNOWN: "compose failed for a reason VCO does not recognise",
}


@dataclass(frozen=True)
class ComposeFailure:
    cause: str
    evidence: str
    remedy: str
    healable: bool
    #: the object the cause is about: socket path, port, network, container name
    subject: str = ""
    #: a container id the stderr named (name conflicts)
    container_id: str = ""

    @property
    def label(self) -> str:
        return CAUSE_LABELS.get(self.cause, self.cause)


# ---------------------------------------------------------------------------
# classify — anchored patterns, ordered by precedence
# ---------------------------------------------------------------------------

_I = re.IGNORECASE | re.MULTILINE
_RE_DOCKER_DAEMON = re.compile(
    r"^.*\bCannot connect to the Docker daemon at\s+(?P<addr>\S+?)\.?\s+Is the docker daemon running\?.*$", _I)
_RE_DIAL_UNIX = re.compile(
    r"^.*\bdial unix\s+(?P<addr>/\S+?):\s+connect:\s+(?:no such file or directory|connection refused)\b.*$", _I)
_RE_PODMAN_CONNECT = re.compile(r"^.*\bCannot connect to Podman\..*$", _I)
_RE_DOCKER_PIPE = re.compile(
    r"^.*\berror during connect:.*(?:docker_engine|dockerDesktopLinuxEngine).*$", _I)
_RE_BUILD_FLAG = re.compile(
    r"^.*\b(?:unrecognized arguments?|unknown flag|unknown shorthand flag|no such option)\b:?[^\n]*--build\b.*$", _I)
_RE_NETWORK_LABEL = re.compile(
    r"^.*\bnetwork\s+(?P<net>[\w.-]+)\s+was found but has incorrect label\s+"
    r"(?P<label>com\.docker\.compose\.[\w.-]+)\b.*$", _I)
_RE_NAME_IN_USE = re.compile(
    r"^.*\bcontainer name\s+\"/?(?P<name>[^\"]+)\"\s+is already in use by\s+"
    r"(?:container\s+)?\"?(?P<id>[0-9a-f]{12,64}\b|an external entity)\"?.*$", _I)
_RE_EXTERNAL_ENTITY = re.compile(r"\balready in use by an external entity\b", re.IGNORECASE)
_RE_PORT = re.compile(
    r"^.*(?:\baddress already in use\b|\bport is already allocated\b).*$", _I)
_RE_PORT_NUMBER = re.compile(
    r"(?:0\.0\.0\.0|\[::\]|::|127\.0\.0\.1|\d{1,3}(?:\.\d{1,3}){3}|localhost):(?P<port>\d{2,5})\b")
_RE_BUILD_FAILED = re.compile(
    r"^.*(?:\bfailed to solve:|\bError: building at STEP\b|\bexecutor failed running\b"
    r"|\bService\s+'?[\w-]+'?\s+failed to build\b|\btarget\s+[\w-]+:\s+failed to solve\b).*$", _I)
_RE_BANNER = re.compile(r"^\s*>>>>.*<<<<\s*$")


def _last_line(stderr: str) -> str:
    lines = [ln.strip() for ln in (stderr or "").splitlines()
             if ln.strip() and not _RE_BANNER.match(ln)]
    return lines[-1] if lines else ""


def _socket_remedy(runtime: str, path: str) -> str:
    where = f" ({path})" if path else ""
    return (f"compose's provider could not reach the {runtime} API socket{where}. "
            "`podman info` does not use that socket, so it can pass while this fails. "
            "Linux: `systemctl --user restart podman.socket`, then confirm the file exists"
            f"{': `ls ' + path + '`' if path else ''}. macOS / Windows: `podman machine start`.")


def daemon_remedy(runtime: str) -> str:
    """The per-OS start recipe for a runtime whose daemon / machine is down."""
    if runtime == "docker":
        return ("Start Docker: Linux `sudo systemctl start docker`; macOS / Windows: start "
                "Docker Desktop and wait for it to settle. Then re-run install.py.")
    return ("Start Podman: macOS / Windows `podman machine start`; Linux "
            "`systemctl --user start podman.socket`. Then re-run install.py.")


def classify(stderr: str, *, provider: Optional[_cp.ComposeProvider] = None,
             runtime: str = "podman") -> ComposeFailure:
    """Map compose's stderr to ONE typed cause. Pure. ``provider`` refines
    wording only; the cause comes from the text."""
    text = stderr or ""
    rt = runtime or (provider.runtime if provider else "podman")

    m = _RE_DOCKER_DAEMON.search(text) or _RE_DIAL_UNIX.search(text)
    if m:
        addr = m.group("addr")
        path = addr[len("unix://"):] if addr.startswith("unix://") else addr
        if rt == "podman":
            return ComposeFailure(SOCKET_MISSING, m.group(0).strip(), _socket_remedy(rt, path),
                                  True, subject=path)
        return ComposeFailure(DAEMON_DOWN, m.group(0).strip(), daemon_remedy(rt), False, subject=addr)
    m = _RE_PODMAN_CONNECT.search(text) or _RE_DOCKER_PIPE.search(text)
    if m:
        return ComposeFailure(DAEMON_DOWN, m.group(0).strip(), daemon_remedy(rt), rt == "podman")

    m = _RE_BUILD_FLAG.search(text)
    if m:
        return ComposeFailure(
            BUILD_FLAG_UNSUPPORTED, m.group(0).strip(),
            "This compose does not accept `--build` on `up`. The stack can come up without it, "
            "but the code_embed image is NOT rebuilt — rebuild it explicitly with a compose "
            "that supports `up --build` (see the command printed below).", True)

    m = _RE_NETWORK_LABEL.search(text)
    if m:
        net = m.group("net")
        return ComposeFailure(
            NETWORK_LABEL_MISMATCH, m.group(0).strip(),
            f"(label mismatch) network {net} was created by a different compose tool. VCO "
            f"removes it when `{rt} ps -a --filter network={net} -q` lists nothing, and "
            "compose recreates it with the right labels. If containers ARE attached they "
            "belong to another compose project: it is left alone — see UPDATE_DEFERRED.md.",
            True, subject=net)

    m = _RE_NAME_IN_USE.search(text)
    if m:
        name = m.group("name")
        cid = m.group("id") if re.fullmatch(r"[0-9a-f]{12,64}", m.group("id") or "") else ""
        if _RE_EXTERNAL_ENTITY.search(text):
            return ComposeFailure(
                NAME_CONFLICT_STORAGE_LEFTOVER, m.group(0).strip(),
                f"A storage-only leftover (see `podman ps -a --external`) still owns the name "
                f"{name}. VCO clears it only when nothing is mounted from it. By hand, in this "
                "order: confirm its layer id is absent from `/proc/self/mountinfo` AND from "
                "`podman unshare cat /proc/self/mountinfo`; remove EMPTY leftover directories "
                "under its overlay `merged/` with plain `rmdir` (never recursively); then "
                f"`podman rm --storage {cid or '<id>'}`.",
                rt == "podman", subject=name, container_id=cid)
        return ComposeFailure(
            NAME_CONFLICT_OTHER_PROVIDER, m.group(0).strip(),
            f"A container named {name} already exists and was not created by this compose "
            f"invocation. `{rt} inspect {name} --format "
            "'{{index .Config.Labels \"com.docker.compose.project\"}}'` names its owner. VCO "
            "never removes it: recreate it with the tool/project that owns it, or remove it "
            "yourself after checking where its data lives.",
            False, subject=name, container_id=cid)

    m = _RE_PORT.search(text)
    if m:
        pm = _RE_PORT_NUMBER.search(m.group(0))
        port = pm.group("port") if pm else ""
        who = (f"`ss -ltnp 'sport = :{port}'` (Linux) or `lsof -iTCP:{port} -sTCP:LISTEN`"
               if port else "`ss -ltnp` (Linux) or `lsof -iTCP -sTCP:LISTEN`")
        return ComposeFailure(
            PORT_TAKEN, m.group(0).strip(),
            f"Another process holds host port {port or '(see the line above)'}: find it with "
            f"{who}. Stop it, or give the VCO service another port from the launcher's "
            "Services page.", False, subject=port)

    m = _RE_BUILD_FAILED.search(text)
    if m:
        return ComposeFailure(
            BUILD_FAILED, m.group(0).strip(),
            "The code_embed image build failed (reason above). It was NOT retried without "
            "`--build`: the running image is unchanged, and its `/health` source_sha still "
            "shows it is not built from this checkout (`vco doctor` re-detects it). Fix the "
            "build error, then re-run the update.", False)

    return ComposeFailure(
        UNKNOWN, _last_line(text),
        "The lines above are compose's own error; VCO does not recognise the cause. Run the "
        "manual command below to see the full output.", False)


def refine(failure: ComposeFailure, *, provider: Optional[_cp.ComposeProvider],
           runtime: str, run: Optional[RunFn] = None) -> ComposeFailure:
    """Upgrade a name conflict to PROVIDER_MISMATCH when the existing
    container's labels positively name the OTHER compose tool (L1-F05).
    Read-only; anything unreadable keeps the original classification."""
    if failure.cause != NAME_CONFLICT_OTHER_PROVIDER or provider is None:
        return failure
    if provider.label_family not in ("docker", "podman"):
        return failure
    ident = _c.compose_identity_of(failure.subject, runtime, run=run)
    if ident is None or not ident.provider or ident.provider == provider.label_family:
        return failure
    return ComposeFailure(
        PROVIDER_MISMATCH, failure.evidence,
        f"{failure.subject} was created by {ident.provider}-compose "
        f"({ident.describe()}), but this run's compose is {provider.engine}. The two tools "
        "cannot manage each other's containers. Recreate it with the tool that created it, "
        "or — after confirming where its data lives — remove it and re-run the update. VCO "
        "does neither on its own.",
        False, subject=failure.subject, container_id=failure.container_id)


# ---------------------------------------------------------------------------
# heal — non-destructive, positive-evidence only
# ---------------------------------------------------------------------------


def _refuse(reason: str, cid: Optional[str] = None, actions: Optional[list] = None) -> HealResult:
    return HealResult(False, list(actions or []), reason, cid)


def _heal_network(net: str, runtime: str, run: RunFn, log: LogFn) -> HealResult:
    try:
        res = run([runtime, "ps", "-a", "--filter", f"network={net}", "-q"],
                  capture_output=True, text=True, timeout=15)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return _refuse(f"could not list containers on network {net}: {exc}")
    if res.returncode != 0:
        return _refuse(f"could not list containers on network {net} "
                       f"(exit {res.returncode}) — left alone")
    attached = [ln.strip() for ln in (res.stdout or "").splitlines() if ln.strip()]
    if attached:
        return _refuse(f"network {net} has {len(attached)} container(s) attached "
                       f"({', '.join(attached[:5])}) — never removed", CID_NETWORK_LABEL_ATTACHED)
    argv = [runtime, "network", "rm", net]
    log(f"  [heal] network {net} has no containers attached — removing it so compose "
        "recreates it with its own labels")
    try:
        rm = run(argv, capture_output=True, text=True, timeout=15)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return _refuse(f"`{' '.join(argv)}` could not run: {exc}", actions=[" ".join(argv)])
    if rm.returncode != 0:
        return _refuse(f"`{' '.join(argv)}` exited {rm.returncode}: "
                       f"{(rm.stderr or '').strip()[:200]}", actions=[" ".join(argv)])
    return HealResult(True, [" ".join(argv)], f"network {net} removed (nothing attached)")


def _read_host_mountinfo() -> str:
    return Path("/proc/self/mountinfo").read_text(encoding="utf-8", errors="replace")


def _layer_of(graphroot: Path, container_id: str) -> Optional[str]:
    import json  # noqa: PLC0415

    path = graphroot / "overlay-containers" / "containers.json"
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(rows, list):
        return None
    for row in rows:
        if isinstance(row, dict) and str(row.get("id", "")) == container_id:
            layer = str(row.get("layer") or "")
            return layer or None
    return None


def _empty_dir_tree(root: Path) -> tuple[bool, str, list[Path]]:
    """``(ok, why, dirs)``: ``dirs`` are the directories strictly under
    ``root``, deepest first, when EVERYTHING under ``root`` is an empty
    directory tree with no mount point and no symlink. Never follows links."""
    if not os.path.lexists(root):
        return True, "", []
    if os.path.islink(root) or not root.is_dir():
        return False, f"{root} is not a plain directory", []
    if os.path.ismount(root):
        return False, f"{root} is a mount point", []
    out: list[Path] = []

    def walk(d: Path) -> Optional[str]:
        try:
            entries = list(os.scandir(d))
        except OSError as exc:
            return f"cannot read {d}: {exc}"
        for entry in entries:
            p = Path(entry.path)
            if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                return f"{p} is not an empty directory (a file or link is left there)"
            if os.path.ismount(p):
                return f"{p} is a mount point"
            why = walk(p)
            if why:
                return why
            out.append(p)
        return None

    why = walk(root)
    if why:
        return False, why, []
    return True, "", out


def _heal_storage_leftover(
    failure: ComposeFailure, runtime: str, run: RunFn, log: LogFn, *,
    read_host_mountinfo: Callable[[], str], graphroot: Optional[Path],
) -> HealResult:
    name = failure.subject
    if runtime != "podman":
        return _refuse("storage-only leftovers are a podman state; not applicable here")
    if name not in _c.CANONICAL_CONTAINERS.values():
        return _refuse(f"{name} is not a VCO container name — never touched",
                       CID_STORAGE_LEFTOVER_UNSAFE)
    rows = _c.list_external_containers(runtime, run=run)
    if rows is None:
        return _refuse("could not list containers with `podman ps -a --external`",
                       CID_STORAGE_LEFTOVER_UNSAFE)
    matches = [r for r in rows if name in r.names]
    if not matches:
        return _refuse(f"no container named {name} in `podman ps -a --external`",
                       CID_STORAGE_LEFTOVER_UNSAFE)
    if len(matches) != 1 or not matches[0].storage_only:
        return _refuse(f"{name} is a container podman still knows (state "
                       f"{matches[0].state or '?'}), not a storage-only leftover",
                       CID_STORAGE_LEFTOVER_UNSAFE)
    row = matches[0]
    hint = failure.container_id
    if hint and not (row.id.startswith(hint) or hint.startswith(row.id)):
        return _refuse(f"the listed id {row.id[:12]} does not match the id compose named "
                       f"({hint[:12]})", CID_STORAGE_LEFTOVER_UNSAFE)
    root = graphroot
    if root is None:
        try:
            info = run([runtime, "info", "--format", "{{.Store.GraphRoot}}"],
                       capture_output=True, text=True, timeout=15)
        except (subprocess.TimeoutExpired, OSError):
            info = None
        if info is None or info.returncode != 0 or not (info.stdout or "").strip():
            return _refuse("could not read podman's storage root", CID_STORAGE_LEFTOVER_UNSAFE)
        root = Path((info.stdout or "").strip())
    layer = _layer_of(root, row.id)
    if not layer:
        return _refuse(f"could not find the storage layer of {row.id[:12]}",
                       CID_STORAGE_LEFTOVER_UNSAFE)
    try:
        host_view = read_host_mountinfo()
    except OSError as exc:
        return _refuse(f"could not read the host mountinfo: {exc}", CID_STORAGE_LEFTOVER_UNSAFE)
    try:
        ns = run([runtime, "unshare", "cat", "/proc/self/mountinfo"],
                 capture_output=True, text=True, timeout=15)
    except (subprocess.TimeoutExpired, OSError):
        ns = None
    if ns is None or ns.returncode != 0:
        return _refuse("could not read the user-namespace mountinfo "
                       "(`podman unshare cat /proc/self/mountinfo`)", CID_STORAGE_LEFTOVER_UNSAFE)
    for label, view in (("host", host_view), ("user namespace", ns.stdout or "")):
        if layer in view or row.id in view:
            return _refuse(f"layer {layer[:12]} is still MOUNTED ({label} mountinfo) — "
                           "never cleaned while mounted", CID_STORAGE_LEFTOVER_UNSAFE)
    merged = root / "overlay" / layer / "merged"
    ok, why, dirs = _empty_dir_tree(merged)
    if not ok:
        return _refuse(f"{why} — refusing: a recursive delete could reach live host data",
                       CID_STORAGE_LEFTOVER_UNSAFE)
    actions: list[str] = []
    for d in dirs:
        try:
            os.rmdir(d)  # non-recursive by construction: it fails on anything non-empty
        except OSError as exc:
            return _refuse(f"rmdir {d} failed: {exc}", CID_STORAGE_LEFTOVER_UNSAFE, actions)
        actions.append(f"rmdir {d}")
    argv = [runtime, "rm", "--storage", row.id]
    log(f"  [heal] {name}: storage-only leftover {row.id[:12]}, nothing mounted — "
        "removing its storage record")
    try:
        rm = run(argv, capture_output=True, text=True, timeout=60)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return _refuse(f"`{' '.join(argv)}` could not run: {exc}", CID_STORAGE_LEFTOVER_UNSAFE,
                       actions + [" ".join(argv)])
    actions.append(" ".join(argv))
    if rm.returncode != 0:
        return _refuse(f"`{' '.join(argv)}` exited {rm.returncode}: "
                       f"{(rm.stderr or '').strip()[:200]}", CID_STORAGE_LEFTOVER_UNSAFE, actions)
    return HealResult(True, actions, f"storage-only leftover {row.id[:12]} removed")


def heal(
    failure: ComposeFailure,
    *,
    runtime: str,
    run: Optional[RunFn] = None,
    log: Optional[LogFn] = None,
    socket_heal: Optional[Callable[[], HealResult]] = None,
    read_host_mountinfo: Optional[Callable[[], str]] = None,
    graphroot: Optional[Path] = None,
) -> HealResult:
    """The non-destructive repair for ``failure``'s cause (module doc)."""
    _run = run or _tsd.run
    _log = log or (lambda msg: print(msg, flush=True))
    if not failure.healable:
        return _refuse(f"{failure.cause}: not healable automatically")
    if failure.cause in (SOCKET_MISSING, DAEMON_DOWN):
        res = (socket_heal or (lambda: _cp.heal_socket(runtime, run=_run)))()
        for act in res.actions:
            _log(f"  [heal] {act}")
        return res
    if failure.cause == NETWORK_LABEL_MISMATCH:
        return _heal_network(failure.subject, runtime, _run, _log)
    if failure.cause == NAME_CONFLICT_STORAGE_LEFTOVER:
        return _heal_storage_leftover(
            failure, runtime, _run, _log,
            read_host_mountinfo=read_host_mountinfo or _read_host_mountinfo,
            graphroot=graphroot,
        )
    return _refuse(f"{failure.cause}: handled by the caller, not a heal")


# ---------------------------------------------------------------------------
# compose up with recovery
# ---------------------------------------------------------------------------


@dataclass
class UpResult:
    returncode: Optional[int]
    stdout: str = ""
    stderr: str = ""
    #: the FIRST failing attempt's stderr — never overwritten by a retry
    first_stderr: str = ""
    heals: list[HealResult] = field(default_factory=list)
    #: the classification of the FIRST failure (None when the first attempt passed)
    failure: Optional[ComposeFailure] = None
    #: the classification of the LAST failure, when it differs from the first
    final_failure: Optional[ComposeFailure] = None
    build_dropped: bool = False
    timed_out: bool = False
    argv: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def compose_up_with_recovery(
    argv: Sequence[str],
    *,
    cwd: Optional[str] = None,
    env: Optional[dict] = None,
    timeout: int = 900,
    runtime: str,
    provider: Optional[_cp.ComposeProvider] = None,
    run: Optional[RunFn] = None,
    compose_run: Optional[RunFn] = None,
    log: Optional[LogFn] = None,
    max_heals: int = 3,
    heal_fn: Callable[..., HealResult] = heal,
) -> UpResult:
    """Run ``argv`` (a full compose ``up`` command); on failure classify,
    heal what is healable, and retry — at most ``max_heals`` heals plus one
    ``--build``-less retry on positive ``BUILD_FLAG_UNSUPPORTED`` evidence.
    ``compose_run`` runs the compose command itself (default
    ``subprocess.run``, looked up at call time); ``run`` the probes/heals."""
    _log = log or (lambda msg: print(msg, flush=True))
    cur = list(argv)
    out = UpResult(returncode=None, argv=cur)
    heals_left = max_heals
    while True:
        runner = compose_run or (lambda a, **kw: subprocess.run(a, **kw))
        try:
            res = runner(cur, capture_output=True, text=True, cwd=cwd, timeout=timeout, env=env)
        except subprocess.TimeoutExpired:
            out.timed_out = True
            out.returncode = None
            return out
        out.returncode, out.stdout, out.stderr, out.argv = (
            res.returncode, res.stdout or "", res.stderr or "", list(cur))
        if res.returncode == 0:
            return out
        failure = refine(classify(out.stderr, provider=provider, runtime=runtime),
                         provider=provider, runtime=runtime, run=run)
        if out.failure is None:
            out.first_stderr, out.failure = out.stderr, failure
        else:
            out.final_failure = failure
        if (failure.cause == BUILD_FLAG_UNSUPPORTED and "--build" in cur
                and not out.build_dropped):
            _log("  compose rejected `--build` (" + failure.evidence[:160]
                 + ") — retrying without it; the image will NOT be rebuilt")
            cur = [a for a in cur if a != "--build"]
            out.build_dropped = True
            continue
        if not failure.healable or failure.cause == BUILD_FLAG_UNSUPPORTED or heals_left <= 0:
            return out
        heals_left -= 1
        h = heal_fn(failure, runtime=runtime, run=run, log=_log)
        out.heals.append(h)
        if not h.healed:
            _log(f"  [heal refused] {h.reason}")
            return out
        _log(f"  [healed] {h.reason} — retrying compose up")


def heal_deferral_entry(h: HealResult, *, manual_cmd: str) -> Optional[DeferralEntry]:
    """The ledger row a refused / failed heal owes (``None`` when it named no
    condition — e.g. a probe that could not run, where nothing is known)."""
    if h.deferral_cid == CID_SOCKET_HEAL_FAILED:
        return DeferralEntry(
            condition_id=CID_SOCKET_HEAL_FAILED,
            title="Container API socket could not be restored",
            detected=f"compose could not reach the podman API socket; VCO ran "
                     f"{', '.join(h.actions) or 'no repair'} and it did not help: {h.reason}",
            why_deferred="The socket unit did not bring the socket file back; the cause is "
                         "outside what a restart can fix (systemd user session, permissions).",
            command_to_apply="systemctl --user status podman.socket\n"
                             "systemctl --user restart podman.socket\n"
                             f"# then re-run:\n{manual_cmd}",
            severity="warning")
    if h.deferral_cid == CID_NETWORK_LABEL_ATTACHED:
        return DeferralEntry(
            condition_id=CID_NETWORK_LABEL_ATTACHED,
            title="Compose network labelled by another tool, with containers attached",
            detected=f"compose refused a network created by a different compose tool: {h.reason}.",
            why_deferred="Removing a network with containers attached would disconnect "
                         "services another compose project owns.",
            command_to_apply="# See who is attached, stop/move them under their own project, "
                             f"then re-run:\n{manual_cmd}",
            severity="warning")
    if h.deferral_cid == CID_STORAGE_LEFTOVER_UNSAFE:
        return DeferralEntry(
            condition_id=CID_STORAGE_LEFTOVER_UNSAFE,
            title="Storage-only container leftover could not be cleared safely",
            detected=f"a storage-only leftover owns a VCO container name; VCO did not remove "
                     f"it: {h.reason}",
            why_deferred="It could not be proven that nothing is mounted from it and that "
                         "only empty directories are left; a wrong delete can reach host data.",
            command_to_apply="podman ps -a --external\n"
                             "# confirm the layer id is absent from /proc/self/mountinfo AND "
                             "`podman unshare cat /proc/self/mountinfo`,\n"
                             "# rmdir (never rm -r) empty leftovers under its merged/, then:\n"
                             f"podman rm --storage <id>\n{manual_cmd}",
            severity="warning")
    return None


def deferral_entries(result: UpResult, *, manual_cmd: str) -> list[DeferralEntry]:
    """The ledger rows a recovery run owes: one per refused/failed heal that
    named a condition, plus a provider mismatch (never auto-resolved)."""
    entries = [e for e in (heal_deferral_entry(h, manual_cmd=manual_cmd)
                           for h in result.heals) if e is not None]
    failures = [f for f in (result.failure, result.final_failure) if f is not None]
    mismatch = next((f for f in failures if f.cause == PROVIDER_MISMATCH), None)
    if mismatch is not None:
        entries.append(DeferralEntry(
            condition_id=CID_PROVIDER_MISMATCH,
            title="Container created by a different compose tool",
            detected=f"{mismatch.evidence} — {mismatch.remedy}",
            why_deferred="Recreating another tool's container needs the data check and your "
                         "consent; VCO never removes it on its own.",
            command_to_apply=f"# after deciding (see detected), re-run:\n{manual_cmd}",
            severity="warning"))
    return entries
