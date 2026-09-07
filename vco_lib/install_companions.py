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

import json
import os
import platform
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, List, Optional, Tuple


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
