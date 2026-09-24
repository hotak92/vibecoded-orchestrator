# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Optional-companion install primitives for install.py (v0.2.75).

D-11 extraction: the lean-ctx discovery-copy helper that install.py used to
host inline. Kept out of the install.py monolith (soft line-ratchet, CLAUDE.md
"extract before you add") — the logic is a pure function of its inputs plus
the OS, with all filesystem effects at the edges.

Since v0.2.92 this module also owns the **editable-install integrity** family
(:func:`classify_vco_lib_origin`, :func:`shadow_repair_plan`, and the thin I/O
edges around them). It lives here rather than in a new module because it is the
same shape as everything else in this file: a pure decision install.py, the
doctor and the tests all call, with the filesystem effects at the boundary.
Three callers, one decision — see :func:`classify_vco_lib_origin`.

This module does NOT import install.py — install.py imports FROM it, keeping
the dependency edge one-directional (install.py -> vco_lib.install_companions).
"""

from __future__ import annotations

import importlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple


#: Reason codes returned by :func:`codegraph_ts_install_plan` when it decides
#: NOT to install (so install.py can log/print a matching notice).
CODEGRAPH_TS_SKIP_ENV = "skip_env"        # VCT_SKIP_CODEGRAPH_TS=1
CODEGRAPH_TS_SKIP_NO_PYPROJECT = "skip_no_pyproject"  # extra pins live there


def codegraph_ts_install_plan(
    *,
    pyproject_exists: bool,
    skip_env: bool,
    project_root: str,
) -> Tuple[bool, Optional[str], Optional[List[str]]]:
    """Decide whether install.py should install the optional ``codegraph-ts``
    extra (tree-sitter call-extraction grammars), and with what pip argv.

    Pure decision function (no I/O, no subprocess) — the testable core of
    install.py's ``_install_codegraph_treesitter`` soft-fail step. install.py
    owns the actual subprocess run + its logging/animation helpers (irreducible
    glue coupled to ``_run_logged_subprocess`` / ``_log_install_event``), so
    only the DECISION lives here.

    Args:
        pyproject_exists: whether ``<project_root>/pyproject.toml`` is present
            (the extra's exact pins live there; nothing to install without it).
        skip_env: whether ``VCT_SKIP_CODEGRAPH_TS=1`` is set (explicit opt-out).
        project_root: the orchestrator root; the pip target is
            ``<project_root>[codegraph-ts]`` so pyproject's pins are the single
            source of truth (no duplicated version list).

    Returns ``(should_install, skip_reason, pip_target_argv)``:
        * ``(False, <reason>, None)`` when skipping — ``skip_reason`` is one of
          the ``CODEGRAPH_TS_SKIP_*`` codes.
        * ``(True, None, ["-e", "<project_root>[codegraph-ts]"])`` when
          installing — the tail argv install.py appends to its pip invocation.

    **``-e`` is load-bearing, not cosmetic (v0.2.92 fix).** ``<root>[extra]``
    names the SAME distribution install step 4 already installed EDITABLY
    (``pip install -e .``). Without ``-e``, pip treats this as a plain install
    of ``vibecoded-orchestrator``: it uninstalls the editable install and
    replaces it with a real COPY of ``vco_lib/`` inside the venv's
    ``site-packages``. Measured shape of the damage (pip 26.2.1, hatchling
    editable layout):

        direct_url.json  {"dir_info": {}, "url": "file:///<root>"}
                         (an editable install carries ``{"editable": true}``)
        RECORD           gains one row per shipped ``vco_lib/`` file
        site-packages    gains a real ``vco_lib/`` directory
        _editable_impl_vibecoded_orchestrator.pth   — GONE

    From then on every hook, MCP and ``python -m vco_lib.X`` invoked outside a
    repo root imports that FROZEN snapshot; a fresh fetch or a bundle update
    changes the checkout and the copy does not, so the user silently keeps
    running install-time code. Because hatchling's editable install is a plain
    path-entry ``.pth`` (``site-packages`` precedes it on ``sys.path``), a
    ``vco_lib/`` directory sitting in ``site-packages`` wins outright.

    Adding ``-e`` both prevents the damage AND repairs it: pip's own
    "Attempting uninstall" step removes the copied files it owns before
    installing the editable, and the extra's dependencies are resolved exactly
    as before (verified empirically, not from the docs).
    """
    if skip_env:
        return (False, CODEGRAPH_TS_SKIP_ENV, None)
    if not pyproject_exists:
        return (False, CODEGRAPH_TS_SKIP_NO_PYPROJECT, None)
    # ``-e <root>[codegraph-ts]`` — pip resolves the extra from pyproject's
    # pins, and ``-e`` is LOAD-BEARING (v0.2.92 fix): see the docstring.
    return (True, None, ["-e", f"{project_root}[codegraph-ts]"])


def ensure_discovered_lean_ctx_on_path(
    found_path: str,
    *,
    home: Path | None = None,
    os_name: str | None = None,
) -> str | None:
    """D-11 (v0.2.75): copy a DISCOVERED lean-ctx binary into ~/.local/bin
    (Windows: %USERPROFILE%\\.cargo\\bin) so a hook shell with a minimal PATH
    resolves it — extending the vendored-copy path to any found binary.

    ``home`` / ``os_name`` are injectable for tests; they default to
    ``Path.home()`` / ``platform.system()`` at call time.

    Returns the destination path when a copy actually happened, None when:
      * the binary is ALREADY on PATH (``shutil.which`` found it) — nothing to do;
      * the binary is already AT the canonical dest — idempotent no-op;
      * the source doesn't exist, or the copy failed (soft-fail — never blocks
        install).

    We do NOT vendor new platform prebuilts here (binary provenance is a
    maintainer decision); we only relocate a binary the user already has.
    """
    import platform

    if home is None:
        home = Path.home()
    if os_name is None:
        os_name = platform.system()
    try:
        src = Path(found_path)
        if not src.is_file():
            return None
        if os_name == "Windows":
            dest_dir = home / ".cargo" / "bin"
            dest = dest_dir / "lean-ctx.exe"
        else:
            dest_dir = home / ".local" / "bin"
            dest = dest_dir / "lean-ctx"
        # Already resolvable on PATH -> the hook's `command -v` finds it; skip.
        if shutil.which("lean-ctx"):
            return None
        # Already at the destination -> idempotent no-op.
        try:
            if dest.exists() and dest.resolve() == src.resolve():
                return None
        except OSError:
            pass
        dest_dir.mkdir(parents=True, exist_ok=True)
        # F-11: overwrite the same ~/.local/bin/lean-ctx dest that
        # install.py's migrated site writes — route through the shared
        # atomic primitive (one-concern-one-home) so a mid-copy crash
        # can't leave a truncated binary on PATH. Reads the (small) binary
        # fully into memory, which atomic_copy_file explicitly supports.
        from vco_lib.atomic import atomic_copy_file  # noqa: PLC0415

        atomic_copy_file(src, dest)
        if os_name != "Windows":
            os.chmod(dest, 0o755)
        return str(dest)
    except (OSError, shutil.Error) as e:
        print(f"  lean-ctx: failed to copy discovered binary to PATH dir: {e}")
        return None


# ---------------------------------------------------------------------------
# Editable-install integrity (v0.2.92)
#
# THE DEFECT this family exists for: install step 4 installs the orchestrator's
# own distribution EDITABLY (``pip install -e .``) so every hook, MCP and
# ``python -m vco_lib.X`` runs the user's CHECKOUT. Any later pip step that
# names the same distribution WITHOUT ``-e`` silently undoes that: pip
# uninstalls the editable install and drops a real copy of ``vco_lib/`` into
# the venv's ``site-packages``. From then on the install runs a frozen
# install-time snapshot, and every subsequent update changes the checkout
# without changing the copy — the user's fixes never take effect and nothing
# says so.
#
# ONE decision, three callers (CLAUDE.md "one concern, one home"):
#   * install.py's step-4 repair       -> classify_vco_lib_origin + shadow_repair_plan
#   * vco_lib.doctor's probe           -> classify_vco_lib_origin
#   * tests                            -> both, with injected facts
#
# The pure decisions take facts, never touch the filesystem, and are the only
# place the rules live. The I/O edges below them are deliberately dumb.
# ---------------------------------------------------------------------------

#: The orchestrator's own Python distribution, as pip knows it (pyproject
#: ``[project] name``). Load-bearing for the repair's ownership gate: we only
#: ever act on THIS distribution.
VCO_DISTRIBUTION_NAME = "vibecoded-orchestrator"

#: ``site-packages`` glob for that distribution's dist-info directory (pip
#: normalises ``-`` to ``_`` in the directory name).
VCO_DIST_INFO_GLOB = "vibecoded_orchestrator-*.dist-info"

#: The single top-level package the distribution ships — pyproject's
#: ``[tool.hatch.build.targets.wheel] packages = ["vco_lib"]``. A directory of
#: this name inside ``site-packages`` is, by construction, a COPY of our
#: package: nothing else on PyPI ships a top-level ``vco_lib``.
VCO_PACKAGE_NAME = "vco_lib"

#: Verdicts of :func:`classify_vco_lib_origin`.
ORIGIN_CHECKOUT = "checkout"          #: healthy — resolves to <install_root>/vco_lib
ORIGIN_SITE_PACKAGES = "site_packages"  #: THE defect — resolves to a venv copy
ORIGIN_FOREIGN = "foreign"            #: resolves to some third location
ORIGIN_UNKNOWN = "unknown"            #: could not be measured — never "fine"


def _norm_path(path) -> str:
    """Comparison-normalised absolute path (``""`` for a falsy input).

    ``realpath`` so a symlinked checkout compares equal to its target, and
    ``normcase`` so the Windows comparison is case-insensitive. Every failure
    arm degrades to the un-resolved string rather than raising — a path we
    cannot stat still compares fine against another un-resolved one.
    """
    if not path:
        return ""
    try:
        return os.path.normcase(os.path.realpath(str(path)))
    except OSError:  # pragma: no cover — realpath rarely raises
        return os.path.normcase(str(path))


def _path_is_within(child, parent) -> bool:
    """True when ``child`` is ``parent`` or lives underneath it."""
    c, pa = _norm_path(child), _norm_path(parent)
    if not c or not pa:
        return False
    if c == pa:
        return True
    return c.startswith(pa.rstrip(os.sep) + os.sep)


def file_url_to_path(url: str) -> str:
    """``file://`` URL -> local filesystem path. ``""`` for anything else.

    PEP 610's ``direct_url.json`` records a local directory install as a
    ``file://`` URL; the repair's ownership gate compares it against the
    install root, so it needs the path form. A VCS/archive URL is not a local
    directory and correctly yields ``""`` (which fails the gate).
    """
    if not isinstance(url, str) or not url.startswith("file:"):
        return ""
    try:
        from urllib.parse import urlparse, unquote  # noqa: PLC0415
        from urllib.request import url2pathname  # noqa: PLC0415

        parsed = urlparse(url)
        return url2pathname(unquote(parsed.path))
    except Exception:  # noqa: BLE001 — a malformed URL is "not a local path"
        return ""


def parse_direct_url(payload) -> Tuple[Optional[bool], str]:
    """PURE. ``(editable_tri_state, url)`` from a parsed PEP 610 payload.

    Tri-state on purpose — ``None`` means "this is not a local-directory
    install, so 'editable' is not a question that applies", NOT "not editable".
    The repair only ever acts on a positive ``True``.

        {"dir_info": {"editable": true}, "url": "file:///root"} -> (True,  url)
        {"dir_info": {},                 "url": "file:///root"} -> (False, url)
        {"vcs_info": {...},              "url": "git+https://"} -> (None,  url)
        <absent / malformed>                                    -> (None,  "")
    """
    if not isinstance(payload, dict):
        return (None, "")
    url = payload.get("url")
    if not isinstance(url, str) or not url:
        return (None, "")
    dir_info = payload.get("dir_info")
    if not isinstance(dir_info, dict):
        return (None, url)
    return (bool(dir_info.get("editable", False)), url)


def classify_vco_lib_origin(
    *,
    origin: Optional[str],
    install_root: str,
    site_packages: Optional[str],
) -> Tuple[str, str]:
    """PURE. Where did ``import vco_lib`` ACTUALLY come from?

    Args:
        origin: ``vco_lib.__file__`` as measured from a NEUTRAL cwd (see
            :func:`measure_vco_lib_origin`). Measuring from the checkout would
            answer a different, useless question — the checkout is on
            ``sys.path`` there via ``''``, so it always looks healthy.
        install_root: the orchestrator clone whose ``vco_lib/`` should win.
        site_packages: the venv's ``purelib``, as the venv itself reports it.

    Returns ``(state, human_detail)`` where state is one of the ``ORIGIN_*``
    codes. ``ORIGIN_UNKNOWN`` is load-bearing: a measurement that did not
    happen must never render as healthy (positive evidence only).
    """
    if not origin:
        return (ORIGIN_UNKNOWN, "vco_lib could not be imported or measured")
    package_dir = os.path.dirname(_norm_path(origin))
    if install_root:
        checkout_pkg = _norm_path(os.path.join(str(install_root), VCO_PACKAGE_NAME))
        if checkout_pkg and package_dir == checkout_pkg:
            return (ORIGIN_CHECKOUT, f"resolves to the checkout ({origin})")
    if site_packages and _path_is_within(origin, site_packages):
        return (
            ORIGIN_SITE_PACKAGES,
            f"resolves to a COPY inside the venv's site-packages ({origin}) "
            f"instead of {os.path.join(str(install_root), VCO_PACKAGE_NAME)} — "
            "every hook and MCP is running frozen install-time code",
        )
    return (
        ORIGIN_FOREIGN,
        f"resolves to {origin}, which is neither this install root's "
        "vco_lib/ nor its venv's site-packages",
    )


def shadow_repair_plan(
    *,
    origin_state: str,
    dist_editable: Optional[bool],
    dist_url: str,
    install_root: str,
    site_packages: str,
    package_dir_exists: bool,
    package_dir_is_symlink: bool,
    package_init_exists: bool,
) -> Tuple[bool, str, str]:
    """PURE. May the leftover ``site-packages/vco_lib/`` be DELETED?

    Returns ``(should_remove, reason, path_to_remove)``. ``reason`` is always
    populated — on a refusal it says exactly which gate was not met, so the
    caller can report why it did nothing.

    This is destructive-adjacent, so every gate is POSITIVE evidence and the
    default is to do nothing:

    1. the origin was MEASURED to be inside site-packages (not inferred);
    2. a site-packages path is known;
    3. the package directory exists and is NOT a symlink (we never follow a
       link out of the venv);
    4. it contains ``__init__.py`` — i.e. it is a regular package that really
       does shadow. A leftover WITHOUT ``__init__.py`` is only a namespace
       portion and loses to the checkout's real package, so deleting it would
       be a change with no benefit;
    5. our distribution is now installed EDITABLY — the working replacement is
       already in place, so removing the copy cannot leave the user with no
       ``vco_lib`` at all;
    6. that editable install points at THIS install root — if another checkout
       owns the venv, this is not our directory to touch.

    Gate 5 is also why the common case never reaches here: when the copy is
    pip-OWNED (RECORD lists its files), ``pip install -e .`` uninstalls it
    itself and there is nothing left to sweep. What survives is the UNOWNED
    residue — a copy whose dist-info was lost or replaced — which pip cannot
    see and therefore never removes.
    """
    if origin_state != ORIGIN_SITE_PACKAGES:
        return (False, f"origin state is {origin_state!r}, not a measured shadow", "")
    if not site_packages:
        return (False, "site-packages path unknown", "")
    if not package_dir_exists:
        return (False, "no vco_lib directory in site-packages", "")
    if package_dir_is_symlink:
        return (False, "site-packages/vco_lib is a symlink — never followed", "")
    if not package_init_exists:
        return (
            False,
            "site-packages/vco_lib has no __init__.py (namespace portion only, "
            "does not shadow)",
            "",
        )
    if dist_editable is not True:
        return (
            False,
            f"{VCO_DISTRIBUTION_NAME} is not positively editable "
            f"(direct_url editable={dist_editable!r}) — no working replacement "
            "to fall back on",
            "",
        )
    dist_path = file_url_to_path(dist_url)
    if not dist_path or _norm_path(dist_path) != _norm_path(install_root):
        return (
            False,
            f"the editable install points at {dist_path or dist_url!r}, not "
            f"{install_root!r} — another checkout owns this venv",
            "",
        )
    return (
        True,
        f"unowned copy of {VCO_PACKAGE_NAME}/ shadows the editable install of "
        f"{VCO_DISTRIBUTION_NAME} at {install_root}",
        os.path.join(site_packages, VCO_PACKAGE_NAME),
    )


# --- I/O edges -------------------------------------------------------------


def resolve_install_venv_python(
    install_root, *, os_name: Optional[str] = None
) -> Optional[Path]:
    """Locate the Python interpreter inside the install's venv.

    Canonical modern layout ``<root>/.venv`` first, then the legacy
    ``<root>/claude_mcp_servers/.venv``. ``None`` when neither exists — callers
    treat that as a soft-fail.

    Extracted from install.py's ``_resolve_venv_python_for_install`` in v0.2.92
    (which now delegates here) because the doctor needs the same answer and
    ``vco_lib`` must not import install.py. ``os_name`` is injectable so the
    Windows layout is testable from a POSIX runner.
    """
    name = os_name if os_name is not None else platform.system()
    windows = str(name).lower().startswith("win")
    sub = "Scripts" if windows else "bin"
    py_name = "python.exe" if windows else "python"
    root = Path(install_root)
    for candidate in (
        root / ".venv" / sub / py_name,
        root / "claude_mcp_servers" / ".venv" / sub / py_name,
    ):
        try:
            if candidate.exists():
                return candidate
        except OSError:  # pragma: no cover — defensive
            continue
    return None


# --- the venv relaunch: who launched us, and which venv we are (v0.2.97) ---
#
# install.py re-execs itself under the install's venv when it was started by
# an interpreter that cannot import the venv's packages (the launcher starts
# it with the system `python3`). Since v0.2.97 that relaunch actually happens
# on POSIX (it used to be skipped: see `is_running_inside_venv`), which moved
# two questions whose answers used to be "this process":
#
#   * "which Python did the user LAUNCH with?" — the venv-drift check in
#     `_venv_triage` compares the venv against it, and `--rebuild-venv` exists
#     so the venv can follow it. After the relaunch `sys.version_info` is the
#     venv's own, so the drift check compared the venv with itself;
#   * "which interpreter builds a NEW venv?" — `sys.executable` is now the
#     venv's python, so a recreate would `rmtree` the tree it runs from and
#     then exec a path it had just deleted.
#
# So the relaunch RECORDS the launching interpreter in the child's env, and
# both answers are read from that record.

#: Loop guard: set on every relaunched child; a child never relaunches again.
ENV_RELAUNCHED = "VCT_INSTALL_RELAUNCHED"
#: The interpreter that started install.py, and its ``X.Y``.
ENV_BASE_PYTHON = "VCT_INSTALL_BASE_PYTHON"
ENV_BASE_PYTHON_VERSION = "VCT_INSTALL_BASE_PYTHON_VERSION"
#: Windows only: the PID of the install.py WAITING for this run
#: (:func:`hand_off`), set on the child it runs. Its presence is the promise
#: that the parent honours :data:`RERUN_OUTSIDE_VENV_EXIT`; the child watches
#: that pid and stops when the parent is killed (:func:`start_parent_watch`).
ENV_PARENT_WAITS = "VCT_INSTALL_PARENT_WAITS"
#: Windows only: the exit code a relaunched child uses to hand a venv rebuild
#: back to its waiting parent, which runs outside the venv. Far from every code
#: install.py itself returns (0, 1, 2).
RERUN_OUTSIDE_VENV_EXIT = 0x5643
#: Windows only: the exit code of a run that stopped because the install.py
#: waiting for it was killed (:func:`start_parent_watch`).
PARENT_GONE_EXIT = 0x5644
#: A fresh nonce per hop, set in the child's env AND passed on its argv as
#: ``RELAUNCH_TOKEN_ARG<token>``. Env is inherited by every descendant; argv
#: reaches only the direct child — so a matching pair proves the record was
#: made for THIS run (:func:`adopt_relaunch`).
ENV_RELAUNCH_TOKEN = "VCT_INSTALL_RELAUNCH_TOKEN"
RELAUNCH_TOKEN_ARG = "--vct-relaunch-token="


def _load_relaunch_env_keys() -> Tuple[str, ...]:
    """The record's keys, from the table the launcher embeds too
    (``install_relaunch_env.toml``). Missing or malformed = a broken install."""
    import tomllib

    table = tomllib.loads((Path(__file__).with_name("install_relaunch_env.toml"))
                          .read_text(encoding="utf-8"))
    return tuple(table["keys"])


#: Every key of the relaunch record — never to be inherited by a later run.
RELAUNCH_ENV_KEYS: Tuple[str, ...] = _load_relaunch_env_keys()


def scrub_install_relaunch_env(env):
    """Remove the relaunch record from ``env`` (a dict or ``os.environ``) and
    return it. Every long-lived child install.py starts gets a scrubbed env:
    the record describes one hop, and a detached child that outlives the run
    (vct-updater -> the relaunched launcher -> its next install.py) would hand
    a stale one on."""
    for key in RELAUNCH_ENV_KEYS:
        env.pop(key, None)
    return env


def detached_child_env() -> dict:
    """``os.environ`` without the relaunch record — the env of a long-lived child."""
    return scrub_install_relaunch_env(os.environ.copy())


def is_running_inside_venv(venv_python, prefix: Optional[str] = None) -> bool:
    """True when THIS process is the venv owning ``venv_python`` (two levels up).

    VENV identity (``sys.prefix``), never the resolved binary: a POSIX venv's
    python is a SYMLINK to its base (macOS framework too), so comparing
    binaries kept every launcher update on /usr/bin/python3.12. Resolved +
    case-normalised: /var -> /private/var, ``C:`` vs ``c:``, ``Scripts\\``.
    """
    def _norm(p) -> str:
        path = Path(p)
        try:
            path = path.resolve()
        except (OSError, RuntimeError):  # unresolvable: compare as spelled
            path = path.absolute()
        return os.path.normcase(str(path))

    current = sys.prefix if prefix is None else prefix
    return _norm(current) == _norm(Path(venv_python).parent.parent)


def mark_relaunch(env: dict, base: Optional[str] = None) -> None:
    """Stamp a relaunched child's ``env``: the loop guard, a fresh token for
    :func:`hand_off` to pass on argv, and — only on the FIRST hop — the base
    interpreter: ``base`` when given, else this process's own. A second hop
    (the rebuild handoff, :func:`reexec_outside_venv`) keeps the original.

    ``base`` exists for the rebuild handoff run straight from an activated
    venv (no relaunch before it): there ``sys.executable`` is the venv's own
    python, and the record must name the interpreter the run is handed TO —
    the same build, so its ``X.Y`` is this process's.
    """
    import secrets

    if os.environ.get(ENV_RELAUNCHED) != "1":
        env[ENV_BASE_PYTHON] = base or sys.executable
        env[ENV_BASE_PYTHON_VERSION] = f"{sys.version_info.major}.{sys.version_info.minor}"
    env[ENV_RELAUNCHED] = "1"
    env[ENV_RELAUNCH_TOKEN] = secrets.token_hex(8)


def launcher_python_version() -> str:
    """``X.Y`` of the interpreter that STARTED install.py — the recorded one in
    a relaunched child, this process's own otherwise."""
    if os.environ.get(ENV_RELAUNCHED) == "1":
        recorded = os.environ.get(ENV_BASE_PYTHON_VERSION, "").strip()
        if recorded:
            return recorded
    return f"{sys.version_info.major}.{sys.version_info.minor}"


def base_python_for_venv() -> str:
    """The interpreter a NEW venv is built with — never a venv's own python.

    The recorded launching interpreter when it still exists (the venv must
    follow the Python the user launched with); otherwise this process's base
    interpreter (``sys._base_executable`` — the base behind a venv, itself
    outside one); ``sys.executable`` only as the last resort.
    """
    if os.environ.get(ENV_RELAUNCHED) == "1":
        recorded = os.environ.get(ENV_BASE_PYTHON, "").strip()
        if recorded and Path(recorded).is_file():
            return recorded
    base = getattr(sys, "_base_executable", "") or ""
    return base if base and Path(base).is_file() else sys.executable


# --- continuing as another interpreter: exec on POSIX, spawn-and-wait on Windows
#
# POSIX ``os.execve`` loads the new program INTO this process: same pid, same
# stdio, and whoever waits on the pid gets the new program's exit status.
# Windows has no such primitive. CPython maps ``os.execv*`` onto the C
# runtime's ``_wexecv*``, which CREATES a new process and ends the caller with
# exit code 0 at once (CPython's own test runner, ruff's launcher and
# python-dotenv all branch around it for exactly that). Every parent waiting on
# install.py's pid — the launcher's update/reinstall runners, install.ps1's
# ``$LASTEXITCODE`` — then saw success whatever the real run did, and
# install.ps1 went on to its post-install step while the install still ran.
# The CRT also joins argv with spaces unquoted, so a path with a space split.
#
# So on Windows this process stays, as a transparent parent: it runs the child
# on its own std handles, waits, and exits with the child's code.


def _exec_replaces_process() -> bool:
    return sys.platform != "win32"


def _std_stream_fds() -> List[Optional[int]]:
    """fds 0/1/2 where open, ``None`` where not. Passed to Popen explicitly:
    with all three ``None``, Popen on Windows sets no STARTF_USESTDHANDLES and
    inherits no handles, so the child is not guaranteed the launcher's pipe."""
    fds: List[Optional[int]] = []
    for fd in (0, 1, 2):
        try:
            os.fstat(fd)
        except OSError:
            fds.append(None)
        else:
            fds.append(fd)
    return fds


def run_child(cmd: List[str], env: dict) -> int:
    """Run ``cmd`` on this process's std streams; return its exit code.

    Ctrl-C reaches every process on the console, the child included, so the
    child decides how to stop; this parent keeps waiting and reports what the
    child did. It never kills the child.
    """
    stdin, stdout, stderr = _std_stream_fds()
    proc = subprocess.Popen(cmd, env=env, stdin=stdin, stdout=stdout, stderr=stderr)
    while True:
        try:
            return proc.wait()
        except KeyboardInterrupt:
            continue


def exit_status(returncode: int) -> int:
    """A child's return code as a status this process can exit with unchanged.

    A POSIX signal death (negative) becomes the shell's ``128 + N``. A Windows
    NTSTATUS such as 0xC000013A (Ctrl-C) does not fit the C ``long`` an exit
    status is converted through there, so it goes as the same 32 bits, signed.
    """
    if returncode < 0:
        return 128 - returncode
    if returncode > 0x7FFFFFFF:
        return (returncode & 0xFFFFFFFF) - 0x1_0000_0000
    return returncode


def _exit_now(code: int) -> None:
    """End this process the way an exec would: flushed, but no ``finally`` /
    atexit work (a replaced process never runs its own)."""
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


def hand_off(executable: str, argv: List[str], env: dict) -> None:
    """Continue this install.py run as ``argv`` under ``executable``.

    POSIX: ``os.execve``. Windows: run it as a child, wait, exit with its code
    — and when the child hands a venv rebuild back (:func:`reexec_outside_venv`),
    run the same command ONCE more under this interpreter, which lives outside
    the venv, and exit with that run's code. Returns only when ``os.execve`` is
    replaced by a recorder (tests).
    """
    token = env.get(ENV_RELAUNCH_TOKEN, "")
    if token:  # the half of the proof only the direct child receives
        argv = [*argv, RELAUNCH_TOKEN_ARG + token]
    if _exec_replaces_process():
        os.execve(executable, argv, env)
        return
    child_env = dict(env)
    child_env[ENV_PARENT_WAITS] = str(os.getpid())
    rc = run_child([executable, *argv[1:]], child_env)
    if rc == RERUN_OUTSIDE_VENV_EXIT:  # honoured once: a second one is just an exit code
        print(f"[vct] venv rebuild handed back: re-running install.py under {sys.executable}")
        sys.stdout.flush()
        rc = run_child([sys.executable, *argv[1:]], child_env)
    _exit_now(exit_status(rc))


def adopt_relaunch(argv: List[str]) -> bool:
    """First thing install.py's ``main()`` does: is this run a relaunch its
    DIRECT parent made? Returns True when it is.

    Always removes the token arguments from ``argv`` (in place — argparse never
    sees them). A relaunch is genuine when ``argv`` carried exactly one token
    and it equals :data:`ENV_RELAUNCH_TOKEN`; then the parent-watch starts.
    Otherwise any relaunch keys in this environment were INHERITED from an
    older run through some other process, and acting on them would skip the
    venv relaunch or watch an unrelated pid: they are removed from
    ``os.environ`` (one stderr line), and the run proceeds as a fresh one.
    """
    tokens = [arg[len(RELAUNCH_TOKEN_ARG):] for arg in argv if arg.startswith(RELAUNCH_TOKEN_ARG)]
    argv[:] = [arg for arg in argv if not arg.startswith(RELAUNCH_TOKEN_ARG)]
    expected = os.environ.get(ENV_RELAUNCH_TOKEN, "")
    if expected and tokens == [expected]:
        start_parent_watch()
        return True
    inherited = [key for key in RELAUNCH_ENV_KEYS if key in os.environ]
    if inherited:
        scrub_install_relaunch_env(os.environ)
        print(f"[vct] ignoring {', '.join(inherited)}: inherited from another run, not set by "
              "the install.py that started this one", file=sys.stderr)
    return False


def waiting_parent_pid() -> Optional[int]:
    """The pid in :data:`ENV_PARENT_WAITS`, or None when no install.py waits."""
    try:
        pid = int(os.environ.get(ENV_PARENT_WAITS, "").strip())
    except ValueError:
        return None
    return pid if pid > 0 else None


def start_parent_watch() -> bool:
    """Windows: stop this run when the install.py waiting for it is killed.

    On POSIX a kill of install.py's pid kills the run itself (the exec kept the
    pid) and never its detached daemons. On Windows that pid is the waiting
    parent (:func:`hand_off`), so a kill of it left the run going. This restores
    the POSIX outcome — the run, not the daemons it started (a kill-on-close Job
    object would also kill vct-updater mid-swap, the hub and the analyzer).

    Pid reuse: only called by :func:`adopt_relaunch` once the argv token has
    proved the pid was set by this run's own parent (an INHERITED pid is
    dropped there, never watched). The handle is opened at startup, while that
    parent is waiting in :func:`run_child` — it only ends before this run does
    when it is killed. So the handle can name another process only when the
    parent was already killed, when stopping is the intent anyway: the worst
    case is a missed stop (the pre-v0.2.97 behaviour), never a wrong one.

    Returns whether a watcher runs. A failed open is reported on stderr and the
    run continues — an install is never aborted because it could not be watched.
    """
    if _exec_replaces_process():
        return False
    pid = waiting_parent_pid()
    if pid is None:
        return False
    try:
        # Any: typeshed declares _winapi for win32 only; this runs only there.
        winapi: Any = importlib.import_module("_winapi")
        handle = winapi.OpenProcess(winapi.SYNCHRONIZE, False, pid)
    except (ImportError, OSError) as exc:
        print(f"[vct] cannot watch the install.py waiting for this run (pid {pid}): {exc}; "
              "continuing — a kill of that process will not stop this run", file=sys.stderr)
        return False
    threading.Thread(target=_stop_when_parent_ends, args=(winapi, handle, pid),
                     name="vct-install-parent-watch", daemon=True).start()
    return True


def _stop_when_parent_ends(winapi: Any, handle, pid: int) -> None:
    winapi.WaitForSingleObject(handle, winapi.INFINITE)
    # The parent waits for this run, so it ending first means it was killed.
    # Stop the way that kill would have on POSIX: at once, no cleanup.
    sys.stderr.write(f"[vct] the install.py waiting for this run (pid {pid}) was killed; "
                     f"stopping this run too (exit {PARENT_GONE_EXIT})\n")
    sys.stderr.flush()
    os._exit(PARENT_GONE_EXIT)


#: install.py flags whose VALUE is a secret (v0.2.97). Any line that echoes
#: or logs a command line goes through :func:`redact_secret_argv` — the ONE
#: redactor (``vco_lib.openai_key`` re-exports it; install.py's session log and
#: :func:`reexec_outside_venv`'s re-run hint both use it).
SECRET_ARGV_FLAGS: tuple = ("--openai-key",)


def redact_secret_argv(argv: List[str]) -> List[str]:
    """``argv`` with the value of every :data:`SECRET_ARGV_FLAGS` flag
    replaced by ``<redacted>`` (both ``--flag VALUE`` and ``--flag=VALUE``)."""
    out: List[str] = []
    redact_next = False
    for arg in argv:
        if redact_next:
            out.append("<redacted>")
            redact_next = False
        elif arg in SECRET_ARGV_FLAGS:
            out.append(arg)
            redact_next = True
        elif any(arg.startswith(f"{flag}=") for flag in SECRET_ARGV_FLAGS):
            out.append(arg.split("=", 1)[0] + "=<redacted>")
        else:
            out.append(arg)
    return out


def reexec_outside_venv(argv, venv_root) -> bool:
    """Continue this install.py run under the base interpreter, so a venv
    rebuild never deletes the tree it runs from.

    Returns False (and does nothing but print why) when it cannot: the only
    interpreter available lives inside ``venv_root`` itself, or — Windows —
    no waiting install.py outside the venv can take the run back (a process
    cannot delete the venv it runs from there, and an exec would not end it:
    see :func:`hand_off`). The caller must then refuse the rebuild. Never
    returns otherwise. The loop guard stays set, so the continued run does not
    relaunch back into the venv it is about to rebuild.
    """
    base = base_python_for_venv()
    root = os.path.normcase(os.path.abspath(str(venv_root)))
    here = os.path.normcase(os.path.abspath(base))
    if here == root or here.startswith(root + os.sep):
        print(f"[vct] no interpreter outside {venv_root} to rebuild it with")
        return False
    if not _exec_replaces_process():
        if waiting_parent_pid() is None:
            shown = redact_secret_argv([base, *argv])
            hint = (" (put your real value back in place of <redacted>)"
                    if shown != [base, *argv] else "")
            print(f"[vct] {venv_root} cannot be rebuilt by a process running from it on "
                  "Windows; re-run it with the base interpreter: "
                  + subprocess.list2cmdline(shown) + hint)
            return False
        print(f"[vct] rebuilding {venv_root}: handing the run back to the install.py "
              "waiting outside the venv it was running from")
        _exit_now(RERUN_OUTSIDE_VENV_EXIT)
        return False  # only reachable when os._exit is replaced (tests)
    env = os.environ.copy()
    mark_relaunch(env, base=base)
    print(f"[vct] rebuilding {venv_root}: relaunching install.py under {base}, "
          "outside the venv it was running from")
    sys.stdout.flush()
    hand_off(base, [base, *argv], env)
    return False  # only reachable when os.execve is replaced (tests)


def vco_lib_origin_script() -> str:
    """Return the ``python -c`` script that reports where ``vco_lib`` resolves.

    Pure builder, same shape as
    ``vco_lib.install_mcp.build_weaviate_mcp_import_verify_script`` (the FN-5b
    "prove the editable install actually took" primitive) — that one asks
    *whether* a package imports, this one asks *from where*. Prints ONE line of
    JSON on stdout: ``{"origin": ..., "purelib": ..., "error": ...}``.
    """
    return (
        "import json, sysconfig\n"
        "origin = ''\n"
        "err = ''\n"
        "try:\n"
        "    import vco_lib\n"
        "    origin = getattr(vco_lib, '__file__', '') or ''\n"
        "except Exception as exc:\n"
        "    err = f'{type(exc).__name__}: {exc}'\n"
        "print(json.dumps({'origin': origin, 'error': err, "
        "'purelib': sysconfig.get_paths().get('purelib', '')}))\n"
    )


def measure_vco_lib_origin(
    venv_python,
    *,
    timeout: int = 60,
    runner: Optional[Callable] = None,
) -> Optional[dict]:
    """Run :func:`vco_lib_origin_script` in ``venv_python`` from a NEUTRAL cwd.

    Returns the parsed payload, or ``None`` when the measurement could not be
    taken (interpreter missing, timeout, unparseable output) — the caller must
    render that as ``unknown``, never as healthy.

    Two deliberate choices about the child environment:

    * **cwd is a fresh temp dir.** Python puts the script's directory on
      ``sys.path``; running from the checkout would find ``vco_lib/`` there no
      matter how broken the install is. A hook fired from an arbitrary project
      folder is the case we actually care about.
    * **``PYTHONPATH`` is scrubbed to ``""``.** Same reasoning (and the same
      empty-string-not-deleted convention) as install.py's
      ``_pip_subprocess_env``; kept separate because that helper also carries
      pip-specific vars and lives in install.py, which ``vco_lib`` must not
      import. Scrubbing measures the WORST case — what a hook shell with no
      PYTHONPATH help sees — which is the state health should be asserted
      against.
    """
    if not venv_python:
        return None
    env = dict(os.environ)
    env["PYTHONPATH"] = ""
    run = runner or subprocess.run
    try:
        with tempfile.TemporaryDirectory(prefix="vco-origin-") as neutral_cwd:
            result = run(
                [str(venv_python), "-c", vco_lib_origin_script()],
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=neutral_cwd,
                env=env,
            )
    except (OSError, subprocess.SubprocessError):
        return None
    stdout = (getattr(result, "stdout", "") or "").strip()
    if not stdout:
        return None
    try:
        payload = json.loads(stdout.splitlines()[-1])
    except (ValueError, IndexError):
        return None
    return payload if isinstance(payload, dict) else None


def read_vco_dist_shape(site_packages) -> Tuple[Optional[bool], str, str]:
    """``(editable_tri_state, url, dist_info_path)`` for OUR distribution.

    ``(None, "", "")`` when the dist-info is absent, unreadable, or AMBIGUOUS
    (more than one matching dist-info directory) — an ambiguous install is one
    we refuse to reason about, which makes the repair's gate 5 fail closed.
    """
    try:
        matches = sorted(Path(site_packages).glob(VCO_DIST_INFO_GLOB))
    except OSError:
        return (None, "", "")
    if len(matches) != 1:
        return (None, "", "")
    direct_url = matches[0] / "direct_url.json"
    try:
        payload = json.loads(direct_url.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return (None, "", str(matches[0]))
    editable, url = parse_direct_url(payload)
    return (editable, url, str(matches[0]))


def repair_shadowed_vco_lib(
    install_root,
    venv_python,
    *,
    measure: Optional[Callable] = None,
    dist_shape: Optional[Callable] = None,
    remove: Optional[Callable] = None,
) -> dict:
    """Detect, and where positively confirmed REPAIR, a shadowed ``vco_lib``.

    Called by install.py immediately after step 4's ``pip install -e .`` — the
    step that owns the editable install and is therefore the one place that
    should verify its own result (the same posture as the FN-5b weaviate_mcp
    import verify a few lines below it).

    Returns a result dict, never raises::

        {"action": "ok"|"skipped"|"removed"|"failed",
         "state": <ORIGIN_* code>, "detail": ..., "reason": ...,
         "removed_path": ..., "verified_state": ...}

    ``action`` semantics:
      * ``ok``      — origin is the checkout; nothing to do.
      * ``skipped`` — a shadow (or an unmeasurable state) was seen but a gate
        was not met; ``reason`` says which. Nothing was changed.
      * ``removed`` — an unowned copy was deleted; ``verified_state`` carries a
        FRESH measurement proving what the install resolves to now.
      * ``failed``  — removal was authorised but the delete errored.

    Every seam (``measure`` / ``dist_shape`` / ``remove``) is injectable so the
    decision table is testable without a venv, a subprocess, or a real delete.
    """
    measure_fn = measure or measure_vco_lib_origin
    payload = measure_fn(venv_python) or {}
    origin = payload.get("origin") or ""
    purelib = payload.get("purelib") or ""
    state, detail = classify_vco_lib_origin(
        origin=origin, install_root=str(install_root), site_packages=purelib
    )
    base = {
        "state": state,
        "detail": detail,
        "reason": "",
        "removed_path": "",
        "verified_state": "",
    }
    if state == ORIGIN_CHECKOUT:
        return {**base, "action": "ok"}
    if state != ORIGIN_SITE_PACKAGES:
        return {
            **base,
            "action": "skipped",
            "reason": payload.get("error") or detail,
        }

    editable, url, _dist_info = (dist_shape or read_vco_dist_shape)(purelib)
    package_dir = Path(purelib) / VCO_PACKAGE_NAME
    try:
        dir_exists = package_dir.is_dir()
        is_symlink = package_dir.is_symlink()
        init_exists = (package_dir / "__init__.py").is_file()
    except OSError as exc:  # pragma: no cover — defensive
        return {**base, "action": "skipped", "reason": f"stat failed: {exc}"}

    should_remove, reason, target = shadow_repair_plan(
        origin_state=state,
        dist_editable=editable,
        dist_url=url,
        install_root=str(install_root),
        site_packages=purelib,
        package_dir_exists=dir_exists,
        package_dir_is_symlink=is_symlink,
        package_init_exists=init_exists,
    )
    if not should_remove:
        return {**base, "action": "skipped", "reason": reason}

    try:
        (remove or shutil.rmtree)(target)
    except (OSError, shutil.Error) as exc:
        return {
            **base,
            "action": "failed",
            "reason": f"{type(exc).__name__}: {exc}",
            "removed_path": target,
        }

    after = measure_fn(venv_python) or {}
    after_state, after_detail = classify_vco_lib_origin(
        origin=after.get("origin") or "",
        install_root=str(install_root),
        site_packages=after.get("purelib") or purelib,
    )
    return {
        **base,
        "action": "removed",
        "reason": reason,
        "removed_path": target,
        "verified_state": after_state,
        "detail": after_detail,
    }


def repair_and_report_vco_lib(
    install_root,
    venv_python,
    *,
    printer: Optional[Callable] = None,
    log_event: Optional[Callable] = None,
    repair: Optional[Callable] = None,
) -> dict:
    """:func:`repair_shadowed_vco_lib` plus the install-time reporting.

    install.py calls THIS (a five-line call site) rather than hosting the
    reporting itself: the install monolith is under a hard line ratchet, and
    the report is a pure function of the repair result — nothing about it needs
    to live next to the pip calls.

    Called immediately after install step 4's ``pip install -e .``, the step
    that OWNS the editable install and is therefore the one place that should
    verify its own result (same posture as the FN-5b weaviate_mcp import
    verify a few lines below it).

    THE BUG this closes (shipped for several releases): the optional
    ``codegraph-ts`` step ran ``pip install <root>[codegraph-ts]`` WITHOUT
    ``-e``. That names the same distribution step 4 had just installed
    editably, so pip uninstalled the editable install and dropped a real copy
    of ``vco_lib/`` into the venv's ``site-packages``. Every hook, MCP and
    ``python -m vco_lib.X`` fired from outside a repo root then imported that
    FROZEN snapshot — and because later updates change the checkout and not the
    copy, users' fixes silently never took effect.

    Two repair legs, in the order they fire:

    1. **pip's own.** ``pip install -e .`` uninstalls the non-editable install
       first, removing every copied file pip OWNS via its RECORD, then installs
       the editable. So an install damaged by the old code is already repaired
       by the time this runs — measured, not assumed: the copied package
       directory is gone and the extras stay installed.
    2. **The sweep** in :func:`repair_shadowed_vco_lib`, for the residue leg 1
       structurally cannot reach: a copy whose dist-info was lost or replaced.
       pip has no RECORD for those files, so it never removes them — and since
       hatchling's editable install is a plain path-entry ``.pth``, the leftover
       still WINS over the checkout. ``CLAUDE.md`` has carried that failure as
       folklore ("re-run install.py and put the repo root first on
       PYTHONPATH"); this is what makes install.py actually fix it.

    ``log_event(status, message, data)`` and ``printer(line)`` are injected so
    install.py keeps ownership of its own logging vocabulary. Soft-fail
    throughout: a measurement we could not take, or a gate we could not
    positively satisfy, changes NOTHING and says why — a silent skip is how
    this defect survived for months.
    """
    out = printer or (lambda _line: None)
    log = log_event or (lambda _s, _m, _d: None)
    out("[4/10] Verifying vco_lib resolves to the checkout ... ")
    try:
        result = (repair or repair_shadowed_vco_lib)(install_root, venv_python)
    except Exception as exc:  # noqa: BLE001 — never break the install over a check
        out(f"  SKIP: vco_lib integrity check raised: {exc}")
        log("warn", f"vco_lib integrity check raised: {exc}", {"error": str(exc)})
        return {"action": "skipped", "reason": str(exc), "state": ORIGIN_UNKNOWN}

    action = result.get("action")
    if action == "ok":
        out("  OK: vco_lib resolves to the checkout.")
        log("ok", "vco_lib resolves to the checkout", result)
    elif action == "removed":
        out(f"  REPAIRED: removed a stale copy that was shadowing the "
            f"checkout ({result.get('removed_path')}); vco_lib now resolves "
            f"from the {result.get('verified_state')}.")
        log("ok", "removed a stale site-packages copy of vco_lib", result)
    elif action == "failed":
        out(f"  WARN: could not remove the stale copy at "
            f"{result.get('removed_path')}: {result.get('reason')} — hooks and "
            "MCPs keep running frozen install-time code until it is gone.")
        log("warn", "stale vco_lib copy could not be removed", result)
    else:
        out(f"  SKIP: vco_lib origin is {result.get('state')} — "
            f"{result.get('reason') or result.get('detail')}")
        log("info", "vco_lib integrity check made no change", result)
    return result
