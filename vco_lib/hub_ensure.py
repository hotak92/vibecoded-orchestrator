# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""ONE home for "find the ``vct-hub`` binary and start it if it is not running".

v0.2.92, ruling **R20** ("one ensure-hub mechanism, not a second"), executed.

Before this module the answer to that single question lived in THREE
hand-maintained copies:

* ``templates/hooks/session-start-ensure-hub.sh``  — ~55 lines of bash
  (``detect_arch`` + ``find_hub_binary`` + the ``nohup`` spawn).
* ``templates/hooks/session-start-ensure-hub.ps1`` — the PowerShell port of
  the same, with its own ``Get-ArchDirName`` / ``Get-HubExeNames`` /
  ``Find-HubBinary`` / ``Start-Process`` quartet.
* ``launcher/src-tauri/src/hub_launcher.rs``       — the Rust chain
  (``find_hub_binary`` + ``find_on_path`` + ``is_executable``).

Each copy carried the SAME four-step chain and each was a fresh chance to
drift — and they had already drifted (the ``.sh`` maps ``aarch64`` to
``linux-arm64``; ``install.py``'s ``_bootstrap_launcher_dist_subdir``
returns ``linux-x64`` unconditionally). Per the repo's cross-language rule
this is **class A**: one Python implementation, invoked cross-language as
``python -m vco_lib.hub_ensure`` — a session-start / launcher-boot path is
user-action-triggered and ms-scale, exactly where a subprocess is affordable.

The discovery chain (unchanged — this module CONSOLIDATES it, it does not
redesign it)
------------------------------------------------------------------------
1. ``$VCT_HUB_BIN``  — explicit override (dev builds, custom installs).
2. The **install-folder copy**, preferred over ``PATH`` since v0.2.63 so a
   stale ``vct-hub`` on ``PATH`` (a leftover dev build, an old global
   install) never wins over the copy that shipped with THIS install:
   ``<extra_dirs...>`` (caller-supplied anchors — the launcher passes the
   directory of its own binary, which only it knows), then
   ``<repo_root>/launcher/dist/<arch>/`` and ``<repo_root>/launcher/dist/``.
3. First ``vct-hub`` on ``$PATH``.
4. ``$HOME/.vct/bin/vct-hub`` (install.py's default install location).

The hub runtime contract (read-only here; PRESERVED exactly)
------------------------------------------------------------
* lockfile — ``<vct_root_dir()>/hub.pid``, single-instance per user. First
  line is the owner PID. Mirrors ``vct_hub::lockfile::pid_path()`` and
  ``launcher/src-tauri/src/hub_status.rs::probe``.
* port — ``$VCT_HUB_PORT`` → ``<vct_root_dir()>/hub.port`` → ``7700``.
* token — ``$VCT_HUB_TOKEN`` → ``<vct_root_dir()>/hub.token``.

The PORT reader lives here — :func:`resolve_hub_port`, moved out of
:func:`vco_lib.project_config._discover_hub` in v0.2.97 so the stdlib-only
callers (``install.py --bootstrap --json``, which runs before any package is
installed, and :mod:`vco_lib.secrets_bootstrap`) report the hub's real port
without importing ``requests``. ``_discover_hub`` calls it, so there is still
one reader. The TOKEN reader stays in :mod:`vco_lib.project_config`.

What this module deliberately does NOT own
------------------------------------------
The **orchestrator update gate** (``<vct_root>/.update-in-progress.json``).
It is the CALLER's concern and the two callers answer it differently on
purpose: the launcher parses the in-JSON deadline
(``commands::update_gate::is_update_in_progress``), while the hooks use a
15-minute mtime proxy because they have no JSON parser. Both check it
BEFORE calling this module.

CLI
---
``python -m vco_lib.hub_ensure resolve [--json|--shell] [--extra-dir DIR]...``
    Print the resolved binary path (or the not-found reason). No spawn.

``python -m vco_lib.hub_ensure ensure [--json|--shell] [--extra-dir DIR]...``
    Probe liveness; spawn ``<bin> --start-if-not-running`` only when the hub
    is not already running. ``--wait`` runs it in the foreground instead of
    detaching (used by the hooks' ``VCO_HOOK_DEBUG=1`` path).

Exit codes follow :mod:`vco_lib.containers`' convention — ``1`` and ``2`` are
deliberately unused, because ``1`` is what the interpreter exits with on an
unhandled exception (an ``import vco_lib`` failure in a BROKEN install) and
``2`` is argparse's usage error. A caller must be able to tell "no hub
binary on this machine" from "the resolver itself could not run":

======  ==================================================================
``0``   ``started`` / ``already_running`` / ``resolved``
``3``   ``binary_not_found`` — loud, named, non-zero.
``4``   ``spawn_failed``     — binary found, exec failed.
======  ==================================================================
"""

from __future__ import annotations

import argparse
import os
import platform
import re
import shutil
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from vco_lib.intfile import read_int_line
from vco_lib.paths import vct_root_dir

__all__ = [
    "DEFAULT_HUB_PORT",
    "binary_names",
    "find_dist_binary",
    "HUB_PID_FILE",
    "HUB_PORT_FILE",
    "HUB_TOKEN_FILE",
    "ENSURE_EXIT_CODES",
    "EnsureState",
    "EnsureResult",
    "dist_arch_dir",
    "ensure_running",
    "find_hub_binary",
    "hub_binary_names",
    "hub_pid",
    "hub_pid_file",
    "hub_port_file",
    "hub_token_file",
    "parse_hub_port",
    "read_hub_port_file",
    "resolve_hub_port",
    "running_hub_port",
    "is_running",
    "main",
]

#: The hub's single-instance lockfile, port file and token file, all under
#: :func:`vco_lib.paths.vct_root_dir`. The port reader is
#: :func:`resolve_hub_port` (below); the token reader lives in
#: :mod:`vco_lib.project_config` — see the module docstring.
HUB_PID_FILE = "hub.pid"
HUB_PORT_FILE = "hub.port"
HUB_TOKEN_FILE = "hub.token"

#: Last-resort port when neither ``$VCT_HUB_PORT`` nor ``hub.port`` answers.
DEFAULT_HUB_PORT = 7700


class EnsureState(str, Enum):
    """Outcome of :func:`ensure_running` / :func:`find_hub_binary`.

    Mirrors ``hub_launcher.rs::SpawnOutcome`` minus the launcher-only
    ``SkippedUpdateInProgress`` (see the module docstring: the update gate
    stays with the caller).
    """

    #: `--start-if-not-running` was invoked and reported success.
    STARTED = "started"
    #: A live hub already owns the lockfile — nothing was spawned.
    ALREADY_RUNNING = "already_running"
    #: `resolve` only: a binary was found, no spawn was requested.
    RESOLVED = "resolved"
    #: No candidate matched any step of the chain.
    BINARY_NOT_FOUND = "binary_not_found"
    #: A binary was found but could not be executed.
    SPAWN_FAILED = "spawn_failed"


#: Exit code per state. See the module docstring for why 1 and 2 are unused.
ENSURE_EXIT_CODES: dict[EnsureState, int] = {
    EnsureState.STARTED: 0,
    EnsureState.ALREADY_RUNNING: 0,
    EnsureState.RESOLVED: 0,
    EnsureState.BINARY_NOT_FOUND: 3,
    EnsureState.SPAWN_FAILED: 4,
}


@dataclass(frozen=True)
class EnsureResult:
    """What happened, why, and which binary it happened to."""

    state: EnsureState
    binary: Optional[str] = None
    pid: Optional[int] = None
    #: Human-readable, always populated — every non-success path is NAMED.
    reason: str = ""

    @property
    def exit_code(self) -> int:
        return ENSURE_EXIT_CODES[self.state]

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "binary": self.binary,
            "pid": self.pid,
            "reason": self.reason,
        }


# ---------------------------------------------------------------------------
# The hub runtime contract — paths only (readers live in project_config).
# ---------------------------------------------------------------------------


def hub_pid_file() -> Path:
    """``<vct_root_dir()>/hub.pid`` — the single-instance lockfile."""
    return vct_root_dir() / HUB_PID_FILE


def hub_port_file() -> Path:
    """``<vct_root_dir()>/hub.port``."""
    return vct_root_dir() / HUB_PORT_FILE


#: The characters a hub-port value may be padded with — C-locale ``isspace``
#: (space, ``\t``, ``\n``, ``\v``, ``\f``, ``\r``). Only LEADING/TRAILING;
#: whitespace inside the value makes it invalid.
_HUB_PORT_PAD = " \t\n\v\f\r"
#: ASCII digits only — ``[0-9]`` in a ``str`` pattern never matches the
#: Unicode ``Nd`` digits that ``int()`` (and ``\d``) accept.
_HUB_PORT_RE = re.compile(r"[0-9]{1,5}")


def parse_hub_port(raw: Optional[str]) -> Optional[int]:
    """The port ``raw`` names, or ``None`` when it is not a valid hub port.

    THE value rule every hub-port reader applies, to ``$VCT_HUB_PORT`` and to
    the whole content of ``hub.port`` alike (R7b F9): after trimming leading /
    trailing whitespace the value is ``[0-9]{1,5}`` in ASCII and in 1..65535.
    No sign (``+7822``), no digit separator (``7_700``), no Unicode digits, no
    internal whitespace (``78 11``, or a second line) — each of those is
    INVALID and the caller falls through to its next source. MUST MATCH the
    other readers, which run the same table (``tests/fixtures/hub_port_cases.json``):
    ``_hub_port_value`` in the bash clients, ``ConvertTo-HubPort`` in the ps1
    clients, ``vct_launcher_core::services::hub_port::parse_hub_port`` and
    ``vct-cli``'s ``valid_hub_port``. Before v0.2.97 Python used ``int()``,
    which accepts all four shapes above, while the shell clients refused them.
    """
    if not isinstance(raw, str):
        return None
    value = raw.strip(_HUB_PORT_PAD)
    if _HUB_PORT_RE.fullmatch(value) is None:
        return None
    port = int(value)
    return port if 1 <= port <= 65535 else None


def read_hub_port_file(vct_root: Optional[Path] = None) -> Optional[int]:
    """STRICT: the port in ``<vct_root>/hub.port``, or ``None``. Never raises.

    For a caller about to TALK to the running hub: no file means no running
    hub, and guessing 7700 would send the request to whatever holds that
    port. MUST MATCH ``vct_launcher_core::services::hub_port::read_hub_port_file_in``
    (missing, unreadable and invalid are all "no port").
    """
    port_file = (vct_root if vct_root is not None else vct_root_dir()) / HUB_PORT_FILE
    try:
        raw = port_file.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    return parse_hub_port(raw)


def running_hub_port(vct_root: Optional[Path] = None) -> int:
    """Where the RUNNING hub listens: ``hub.port`` → ``$VCT_HUB_PORT`` → 7700.

    File-first on purpose — this DESCRIBES the hub rather than pinning a
    client to one: the hub walks past a taken port (``server.rs::try_bind``)
    and writes the port it bound, so only the file knows. MUST MATCH
    ``vct_launcher_core::services::hub_port::resolve_hub_port`` (the Rust
    "describe the running hub" reader). Use :func:`resolve_hub_port` for a
    client that honours an explicit ``$VCT_HUB_PORT`` pin.
    """
    from_file = read_hub_port_file(vct_root)
    if from_file is not None:
        return from_file
    from_env = parse_hub_port(os.environ.get("VCT_HUB_PORT"))
    return from_env if from_env is not None else DEFAULT_HUB_PORT


def resolve_hub_port(
    vct_root: Optional[Path] = None,
    warn: Optional[Callable[[str, str], None]] = None,
) -> int:
    """The hub's port for a CLIENT: ``$VCT_HUB_PORT`` → ``<vct_root>/hub.port`` → 7700.

    The ONE Python client port reader. Callers: ``vco_lib.project_config._discover_hub``,
    ``vco_lib.access_resolver._hub_port``, ``vco_lib.codegraph_resync``,
    ``claude_mcp_servers/rl_client/hub_writer._read_hub_port``,
    ``claude_mcp_servers/wrappers/_base`` and ``weaviate_mcp/server``'s access
    lookup, and ``vco verify-diagrams`` (check 5). Stdlib-only, so it works
    before the install has installed anything. ``vct_root`` defaults to
    :func:`vco_lib.paths.vct_root_dir`. ``install.py --bootstrap --json`` and
    :mod:`vco_lib.secrets_bootstrap` DESCRIBE the hub and use
    :func:`running_hub_port`; install.py's health probe talks to it and uses
    the strict :func:`read_hub_port_file`.

    A valid port is what :func:`parse_hub_port` accepts.

    F-8 corrupt-input contract — MUST MATCH the bash sibling
    ``vct_project_config.sh::hub_port`` and the ps1 sibling
    ``vct_project_config.ps1::Get-HubPort``: nothing here raises.

    * ``VCT_HUB_PORT`` set but not a valid port → ``warn("hub_port_invalid")``
      and FALL THROUGH to ``hub.port``, then the default (owner ruling
      2026-09-24: the file names the RUNNING hub, which beats a guess; before
      v0.2.97 this jumped straight to 7700, and three other readers fell
      through to the file — the readers disagreed).
    * ``hub.port`` unreadable → ``warn("hub_port_unreadable")`` + default;
      non-empty but not a valid port → ``warn("hub_port_invalid")`` + default;
      absent or empty → the silent default.

    ``warn`` defaults to silence (the bootstrap JSON's stdout is a contract);
    ``project_config`` passes its stderr warner.
    """
    def _warn(kind: str, detail: str) -> None:
        if warn is not None:
            warn(kind, detail)

    port_env = os.environ.get("VCT_HUB_PORT", "")
    if port_env.strip(_HUB_PORT_PAD):
        from_env = parse_hub_port(port_env)
        if from_env is not None:
            return from_env
        _warn(
            "hub_port_invalid",
            "VCT_HUB_PORT is not a port (1-65535); falling back to hub.port, then 7700",
        )
    port_file = (vct_root if vct_root is not None else vct_root_dir()) / HUB_PORT_FILE
    try:
        raw = port_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        return DEFAULT_HUB_PORT
    except (OSError, ValueError):
        _warn("hub_port_unreadable", "hub.port is not readable; using default 7700")
        return DEFAULT_HUB_PORT
    # The PARSE is shared (:func:`parse_hub_port`); the classification above
    # is not, and must not be — an unreadable file and a file of nonsense
    # emit DIFFERENT warnings, and that difference is the cross-language
    # contract with the .sh/.ps1 siblings.
    parsed = parse_hub_port(raw)
    if raw.strip(_HUB_PORT_PAD) and parsed is None:
        _warn("hub_port_invalid", "hub.port contains non-integer content; using default 7700")
    return parsed if parsed is not None else DEFAULT_HUB_PORT


def hub_token_file() -> Path:
    """``<vct_root_dir()>/hub.token``."""
    return vct_root_dir() / HUB_TOKEN_FILE


def hub_pid() -> Optional[int]:
    """Owner PID from the lockfile, or ``None``.

    Byte-for-byte the same reading as ``hub_status.rs::probe``: first line,
    trimmed, parsed as an unsigned int. A missing, empty, unreadable or
    unparseable lockfile is a regular ``None`` (the hub's own ``acquire()``
    overwrites it on next start) — never an exception.
    """
    # `hub_status.rs` parses into u32, and the shell hooks' `pid_alive`
    # rejects 0 explicitly. Both mean: not a startable owner — which is what
    # `minimum=1` says to the shared reader.
    return read_int_line(hub_pid_file(), minimum=1)


def is_running() -> bool:
    """True when the lockfile names a PID that is still alive.

    A stale lockfile (owner dead — the crash-recovery state) reads as NOT
    running, so the caller starts a fresh hub. Liveness uses the ONE
    cross-OS probe, :func:`vco_lib.deferral_probes.pid_is_alive`, rather
    than a second ``os.kill`` copy (the Windows footgun is worth getting
    wrong in only one place).
    """
    pid = hub_pid()
    if pid is None:
        return False
    from vco_lib.deferral_probes import pid_is_alive

    return pid_is_alive(pid)


def probe_hub_health(timeout: float = 0.5, vct_root: Optional[Path] = None) -> bool:
    """GET the hub's ``/api/v1/health`` on the port ``hub.port`` names.

    True when the hub responds with status < 400, False otherwise. The port
    comes from the STRICT file-only reader (:func:`read_hub_port_file`): no
    file / not a valid port means no running hub to probe, and guessing 7700
    would send the request to whatever holds that port. The install-time
    twin of ``vco_lib.deferral_probes.hub_answers_health`` (same endpoint,
    same intentionally-AUTH-FREE request — the probe runs before the hub
    token file is readable — same ``status < 400`` bar); that one resolves
    the port through the client chain (env → file → 7700), this one through
    the file alone, because the installer cannot trust an inherited pin.
    Moved from install.py's ``_probe_vct_hub_health`` (v0.2.97) so the body
    has one home; install.py keeps a thin shim under the old name.

    v0.2.43 V0243-1: the endpoint is ``/api/v1/health`` (the hub's actual
    health route; ``/health`` 404s on every vct-hub shipped since v0.2.21).

    On a successful probe any pre-existing ``v0.2.21-cutover.flag`` next to
    ``hub.port`` is unlinked — best-effort cleanup of a stale migration
    sentinel an interrupted v0.2.21 install may have left behind.

    Soft-fail on every error (file missing, port invalid, connection
    refused, timeout): a health probe is never a crash path.
    """
    root = vct_root if vct_root is not None else vct_root_dir()
    try:
        port = read_hub_port_file(root)
        if port is None:
            return False
        url = f"http://127.0.0.1:{port}/api/v1/health"
        resp = urllib.request.urlopen(url, timeout=timeout)
        healthy = resp.status < 400
        if healthy:
            # V0243-1: unlink stale v0.2.21-cutover.flag when hub is up.
            legacy_flag = root / "v0.2.21-cutover.flag"
            try:
                if legacy_flag.is_file():
                    legacy_flag.unlink()
            except OSError:
                pass  # Best-effort; not critical.
        return healthy
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Binary discovery.
# ---------------------------------------------------------------------------


def binary_names(stem: str) -> tuple[str, ...]:
    """Candidate filenames for ``stem``, most-likely first.

    Windows probes ``<stem>.exe`` then the bare name; POSIX the reverse —
    matching ``Get-HubExeNames`` in the ``.ps1`` hook and
    ``hub_binary_name()`` in ``hub_launcher.rs``.

    v0.2.95: parameterised over the stem so the LAUNCHER binary
    (:mod:`vco_lib.launcher_ensure`) reads the same two-name rule instead of
    carrying a second copy of it. ``vct-hub`` stays the only caller that has
    a named wrapper, because it is the only one with three callers to keep
    honest.
    """
    if os.name == "nt":
        return (f"{stem}.exe", stem)
    return (stem, f"{stem}.exe")


def hub_binary_names() -> tuple[str, ...]:
    """Candidate ``vct-hub`` filenames — :func:`binary_names` for the hub."""
    return binary_names("vct-hub")


def dist_arch_dir() -> Optional[str]:
    """Name of this host's ``launcher/dist/<arch>/`` slot, or ``None``.

    The real slots are ``linux-x64``, ``macos-arm64``, ``macos-x64`` and
    ``windows-x64``; a local cargo build on an unusual host can also produce
    ``linux-arm64`` (which the ``.sh`` hook has always probed).

    .. note::
       ``install.py::_bootstrap_launcher_dist_subdir`` is a PRE-EXISTING
       sibling of this function (it hardcodes ``linux-x64`` and so is blind
       to a local arm64 Linux build). It belongs here too, but ``install.py``
       was outside this change's file set — migrating it is a named
       follow-up, not a second copy introduced by this module.
    """
    machine = (platform.machine() or "").lower()
    if machine in ("x86_64", "amd64"):
        arch = "x64"
    elif machine in ("aarch64", "arm64"):
        arch = "arm64"
    else:
        arch = machine
    if not arch:
        return None
    system = (platform.system() or "").lower()
    if system == "linux":
        return f"linux-{arch}"
    if system == "darwin":
        return f"macos-{arch}"
    if system == "windows":
        return f"windows-{arch}"
    return None


def _is_executable(path: Path) -> bool:
    """Is ``path`` an executable regular file?

    Same test as ``hub_launcher.rs::is_executable``: a regular file, plus
    an execute bit on POSIX. Windows checks existence only (file
    association decides runnability there).
    """
    try:
        if not path.is_file():
            return False
    except OSError:
        return False
    if os.name == "nt":
        return True
    return os.access(path, os.X_OK)


def _first_executable_in(
    directory: Path, names: Sequence[str] = ()
) -> Optional[Path]:
    for name in (names or hub_binary_names()):
        candidate = directory / name
        if _is_executable(candidate):
            return candidate
    return None


def find_dist_binary(
    stem: str,
    *,
    env_override: Optional[str] = None,
    repo_root: Optional[Path] = None,
    extra_dirs: Sequence[Path] = (),
    probe_repo_dist: bool = True,
    env: Optional[Mapping[str, str]] = None,
) -> Optional[Path]:
    """Resolve a VCO ``launcher/dist/`` binary by stem, or ``None``.

    The four-step chain documented at module level, with the binary stem and
    its override env var as parameters. v0.2.95 extracted it from
    :func:`find_hub_binary` when :mod:`vco_lib.launcher_ensure` needed the
    SAME chain for ``vct-launcher``: the alternative was a fifth hand-written
    copy of a walk that had already drifted once across its first three.

    Never raises and never spawns anything.

    :param stem: binary stem, e.g. ``"vct-hub"`` / ``"vct-launcher"``.
    :param env_override: name of the env var that pins an explicit path
        (step 1). ``None`` skips step 1 entirely.
    :param env: environment to read steps 1 and 4 from. Defaults to the real
        one; a caller that already resolved an environment (the launcher
        ensure threads one through every gate) passes it so the whole
        resolution answers ONE environment rather than two.
    """
    names = binary_names(stem)
    environ: Mapping[str, str] = os.environ if env is None else env

    # 1. Explicit override.
    override = environ.get(env_override, "").strip() if env_override else ""
    if override:
        candidate = Path(override)
        if _is_executable(candidate):
            return candidate
        # Falls through deliberately, matching all three prior copies.

    # 2. Install-folder copy — caller anchors first, then the checkout's
    #    arch-qualified dist slot, then the arch-less fallback.
    for directory in extra_dirs:
        found = _first_executable_in(Path(directory), names)
        if found is not None:
            return found

    root = Path(repo_root) if repo_root is not None else _default_repo_root()
    if probe_repo_dist and root is not None:
        dist = root / "launcher" / "dist"
        arch = dist_arch_dir()
        if arch:
            found = _first_executable_in(dist / arch, names)
            if found is not None:
                return found
        found = _first_executable_in(dist, names)
        if found is not None:
            return found

    # 3. PATH.
    for name in names:
        on_path = shutil.which(name)
        if on_path and _is_executable(Path(on_path)):
            return Path(on_path)

    # 4. Known user-install location.
    home = environ.get("USERPROFILE") if os.name == "nt" else None
    home = home or environ.get("HOME") or ""
    if home:
        found = _first_executable_in(Path(home) / ".vct" / "bin", names)
        if found is not None:
            return found

    return None


def find_hub_binary(
    repo_root: Optional[Path] = None,
    extra_dirs: Sequence[Path] = (),
    probe_repo_dist: bool = True,
) -> Optional[Path]:
    """Resolve the ``vct-hub`` binary, or ``None``.

    Walks the four-step chain documented at module level. Never raises and
    never spawns anything.

    :param repo_root: the orchestrator checkout whose ``launcher/dist/``
        holds the install-folder copy. Defaults to the checkout this module
        is imported from, which is what the hooks want.
    :param extra_dirs: additional install-folder anchors probed BEFORE
        ``repo_root``'s dist dirs. The launcher passes the directory of its
        own running binary (and its parent) here — a fact only the launcher
        process knows, which is why it is an argument rather than something
        this module tries to derive.
    :param probe_repo_dist: whether step 2 also probes
        ``<repo_root>/launcher/dist/``.

        The two callers anchor step 2 DIFFERENTLY, on purpose, and flattening
        that difference would be a behaviour change rather than a merge:

        * the **hooks** know only the checkout, so they probe
          ``<repo_root>/launcher/dist/<arch>/`` — this is their step 2, and
          the default ``True`` preserves it;
        * the **launcher** knows where its OWN binary lives and probes that
          instead (``--extra-dir``). In a shipped install those are the same
          directory; in a dev tree they are NOT, and letting the launcher
          fall back to the checkout's ``dist/`` would hand a `cargo run`
          launcher a hub it never used to find. So it passes ``False``.
    """
    return find_dist_binary(
        "vct-hub",
        env_override="VCT_HUB_BIN",
        repo_root=repo_root,
        extra_dirs=extra_dirs,
        probe_repo_dist=probe_repo_dist,
    )


def _default_repo_root() -> Optional[Path]:
    """The orchestrator checkout containing this module (``<root>/vco_lib/``)."""
    try:
        return Path(__file__).resolve().parent.parent
    except OSError:
        return None


# ---------------------------------------------------------------------------
# "…and start it if it is not running".
# ---------------------------------------------------------------------------


def _spawn(binary: Path, wait: bool) -> EnsureResult:
    """Invoke ``<binary> --start-if-not-running``.

    Detached by default: a slow first start must not block the SessionStart
    hook bus past its 10 s budget, nor the launcher's boot. The hub itself
    short-circuits when already running and returns within ~100 ms, so the
    cost of the spawn is bounded either way.
    """
    from vco_lib.install_companions import detached_child_env

    argv = [str(binary), "--start-if-not-running"]
    env = detached_child_env()  # the hub outlives us: no relaunch record
    creationflags = 0
    start_new_session = False
    if os.name == "nt":
        # DETACHED_PROCESS | CREATE_NO_WINDOW — no conhost.exe flash when the
        # parent is a GUI subsystem process.
        creationflags = 0x0000_0008 | 0x0800_0000
    else:
        start_new_session = True
    try:
        if wait:
            completed = subprocess.run(
                argv,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if completed.returncode != 0:
                return EnsureResult(
                    state=EnsureState.SPAWN_FAILED,
                    binary=str(binary),
                    reason=(
                        f"vct-hub --start-if-not-running exited "
                        f"{completed.returncode}"
                    ),
                )
        else:
            subprocess.Popen(  # noqa: S603 — argv is a resolved absolute path
                argv,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=start_new_session,
                creationflags=creationflags,
            )
    except OSError as exc:
        return EnsureResult(
            state=EnsureState.SPAWN_FAILED,
            binary=str(binary),
            reason=f"could not execute {binary}: {exc}",
        )
    return EnsureResult(
        state=EnsureState.STARTED,
        binary=str(binary),
        reason="vct-hub --start-if-not-running invoked",
    )


def ensure_running(
    repo_root: Optional[Path] = None,
    extra_dirs: Sequence[Path] = (),
    wait: bool = False,
    probe_repo_dist: bool = True,
) -> EnsureResult:
    """Start the hub unless it is already running. Idempotent.

    The leave-alone case is FIRST and is decided from the lockfile, so a
    live hub is never re-spawned. (``--start-if-not-running`` would
    short-circuit anyway; checking here means the caller gets an honest
    ``already_running`` answer and the pid, instead of an opaque
    ``started``.)

    Callers must check the orchestrator update gate themselves — see the
    module docstring.
    """
    pid = hub_pid()
    if pid is not None and is_running():
        return EnsureResult(
            state=EnsureState.ALREADY_RUNNING,
            pid=pid,
            reason=f"vct-hub already running (pid {pid})",
        )

    binary = find_hub_binary(
        repo_root=repo_root, extra_dirs=extra_dirs,
        probe_repo_dist=probe_repo_dist,
    )
    if binary is None:
        return EnsureResult(
            state=EnsureState.BINARY_NOT_FOUND,
            reason=(
                "vct-hub binary not found: no $VCT_HUB_BIN, no copy under "
                "launcher/dist/, none on $PATH, none in ~/.vct/bin "
                "(set VCT_HUB_BIN to override, or run install.py to deploy it)"
            ),
        )
    return _spawn(binary, wait=wait)


# ---------------------------------------------------------------------------
# CLI — the cross-language call surface (class A of the A>B>C rule).
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m vco_lib.hub_ensure",
        description=(
            "Resolve the vct-hub binary and start it if it is not running "
            "(the ONE home; hooks and the launcher call this instead of "
            "mirroring the logic)."
        ),
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, help_text in (
        ("resolve", "Locate the hub binary. Never spawns. 0=found, 3=not found."),
        ("ensure", "Start the hub unless already running. 0=ok, 3=no binary, 4=spawn failed."),
    ):
        s = sub.add_parser(name, help=help_text)
        s.add_argument("--json", action="store_true", help="Print the result as JSON on stdout.")
        s.add_argument(
            "--shell", action="store_true",
            help="Print `VCO_HUB_*` assignments for a bash `eval` (the .sh hook); "
                 "the .ps1 hook uses --json + ConvertFrom-Json.",
        )
        s.add_argument(
            "--extra-dir", action="append", default=[], metavar="DIR",
            help="Extra install-folder anchor, probed before launcher/dist/. Repeatable.",
        )
        s.add_argument(
            "--repo-root", default=None, metavar="DIR",
            help="Orchestrator checkout whose launcher/dist/ holds the hub copy.",
        )
        s.add_argument(
            "--no-repo-dist", action="store_true",
            help="Do NOT probe <repo-root>/launcher/dist/. The launcher passes "
                 "this: it anchors step 2 on its OWN binary's directory "
                 "(--extra-dir), and must not gain the checkout's dist/ as a "
                 "new discovery source.",
        )
        if name == "ensure":
            s.add_argument(
                "--wait", action="store_true",
                help="Run the spawn in the foreground and report its exit code "
                     "(default: detach, so a slow start cannot block the caller).",
            )
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    import json
    import shlex

    args = _build_arg_parser().parse_args(argv)
    repo_root = Path(args.repo_root) if args.repo_root else None
    extra_dirs = [Path(d) for d in args.extra_dir]

    if args.cmd == "resolve":
        binary = find_hub_binary(
            repo_root=repo_root,
            extra_dirs=extra_dirs,
            probe_repo_dist=not args.no_repo_dist,
        )
        if binary is None:
            res = EnsureResult(
                state=EnsureState.BINARY_NOT_FOUND,
                reason=(
                    "vct-hub binary not found: no $VCT_HUB_BIN, no copy under "
                    "launcher/dist/, none on $PATH, none in ~/.vct/bin"
                ),
            )
        else:
            res = EnsureResult(
                state=EnsureState.RESOLVED,
                binary=str(binary),
                reason=f"resolved {binary}",
            )
    else:
        res = ensure_running(
            repo_root=repo_root, extra_dirs=extra_dirs, wait=args.wait,
            probe_repo_dist=not args.no_repo_dist,
        )

    if args.json:
        print(json.dumps(res.to_dict(), sort_keys=True))
    elif args.shell:
        print(f"VCO_HUB_STATE={shlex.quote(res.state.value)}")
        print(f"VCO_HUB_BINARY={shlex.quote(res.binary or '')}")
        print(f"VCO_HUB_PID={shlex.quote(str(res.pid) if res.pid else '')}")
        print(f"VCO_HUB_REASON={shlex.quote(res.reason)}")
    else:
        print(f"{res.state.value}: {res.binary or '-'} ({res.reason})")

    # Every non-success path is LOUD: the reason also goes to stderr so a
    # caller that only forwards stderr (the hooks) still says what happened.
    if res.exit_code != 0:
        print(f"[vct] hub_ensure: {res.reason}", file=sys.stderr)
    return res.exit_code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
