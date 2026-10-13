# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The default ``playwright`` MCP: its pinned launch, its browser, its
pre-cache and the doctor probe that checks them (v0.2.102 W6-Z4).

The defect this module closes had three compounding parts:

1. **Wrong browser kind.** The entry was ``npx -y @playwright/mcp@latest``
   with no ``--browser``. ``@playwright/mcp`` then launches the ``chrome``
   *channel* (``validateBrowserConfig`` in its playwright-core sets
   ``channel = "chrome"`` when no browser is named) — a system Google Chrome,
   which VCO never installs. The Chromium VCO pre-cached was never used.
2. **Revision drift.** The pre-cache ran ``npx playwright install chromium``,
   which installs the revision of a SEPARATELY resolved ``playwright``; the
   MCP asks for the revision in ITS OWN bundled playwright-core
   (``browsers.json``). With ``@latest`` floating, every resolution could move
   the target (1208 → 1243 → 1247 was observed on one machine).
3. **Blind doctor.** Only "does npx resolve" was checked.

So, one home for each fact:

* **The pin** lives in ``vco_lib/bundled_mcp_versions.toml`` ``[npx.playwright_mcp]``
  (package, exact version, npm shasum, browser) — the table the Rust launcher
  also parses (``bundled_versions.rs::BUNDLED_NPX``). A release bump is one
  edit there.
* **The registration** is :func:`registration_args` /
  :func:`registration_entry`: ``npx -y @playwright/mcp@<pin> --browser
  chromium``. ``--browser chromium`` maps to the ``chrome-for-testing``
  channel, which playwright-core aliases to its own downloaded
  ``chromium-<rev>`` — the browser the pre-cache fetches. Every install /
  ``--update`` rewrites the entry (both writers replace
  ``mcpServers.playwright`` wholesale), so an existing install moves off
  ``@latest`` on its next ordinary update.
* **The expected browser revision** is never pinned by hand: it is read from
  the pinned package's own ``playwright-core/browsers.json`` in the npx cache
  (:func:`cached_package` / :func:`expected_revisions`).
* **The pre-cache** (:func:`precache`) installs through the pinned package's
  own installer (``install-browser``, present in 0.0.83's ``cli.js``), or —
  if a future pin drops that subcommand — through ``playwright@<the pinned
  package's exact playwright-core version>``. Either way the revision matches
  what the MCP will request.
* **The probe** (:func:`browser_state`) answers present / missing / unknown
  with positive evidence only: ``unknown`` whenever the pinned package is not
  in the npx cache (its revision cannot be read), never a guess.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

#: The ``[npx.<key>]`` table this module reads.
PIN_SECTION = "npx"
PIN_KEY = "playwright_mcp"

#: Opt-out of the eager browser download (pre-existing contract, kept).
SKIP_ENV = "VCT_SKIP_PLAYWRIGHT"

#: Registered deferral condition (``vco_lib/deferral_conditions.toml``).
CID_BROWSER_MISSING = "playwright_browser_missing"

#: The pre-v0.2.102 registration. Still recognised as VCO's own (uninstall
#: scrub, the doctor's "registration predates the pin" reading) because an
#: install that has not run its next update still carries it.
LEGACY_ARGS: tuple[str, ...] = ("-y", "@playwright/mcp@latest")

#: Per-step timeouts (seconds). Deliberately long — the owner rule is "very
#: long per-chunk guards, never a global install timeout": a ~170 MB browser
#: on a slow link must not be cut off because a dev machine is fast.
PACKAGE_FETCH_TIMEOUT_S = 900
BROWSER_INSTALL_TIMEOUT_S = 3600

STATE_PRESENT = "present"
STATE_MISSING = "missing"
STATE_UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# The pin
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Pin:
    package: str
    version: str
    shasum: str
    browser: str

    @property
    def spec(self) -> str:
        """``<package>@<exact version>`` — the npx package spec."""
        return f"{self.package}@{self.version}"


def load_pin(manifest: Optional[Mapping[str, Any]] = None) -> Pin:
    """The pinned Playwright MCP from ``bundled_mcp_versions.toml``.

    A missing table or field is a BROKEN install (the file ships with every
    checkout), so it raises ``KeyError`` naming the file — never a silent
    fallback to a floating ``@latest``, which is the defect this pin exists to
    remove.
    """
    from vco_lib import bundled_versions

    data = manifest if manifest is not None else bundled_versions.load_bundled_versions()
    section: Any = data.get(PIN_SECTION, {})
    spec: Any = section.get(PIN_KEY) if isinstance(section, Mapping) else None
    if not isinstance(spec, Mapping):
        raise KeyError(
            f"[{PIN_SECTION}.{PIN_KEY}] missing from "
            f"{bundled_versions.manifest_path()} — the Playwright MCP pin ships "
            "with every checkout; re-fetch the file."
        )
    missing = [k for k in ("package", "version", "shasum", "browser") if not spec.get(k)]
    if missing:
        raise KeyError(
            f"[{PIN_SECTION}.{PIN_KEY}] in {bundled_versions.manifest_path()} "
            f"is missing {missing}."
        )
    return Pin(
        package=str(spec["package"]),
        version=str(spec["version"]),
        shasum=str(spec["shasum"]),
        browser=str(spec["browser"]),
    )


def registration_args(pin: Optional[Pin] = None) -> list[str]:
    """The registered argv after ``npx``.

    MUST match ``vct-launcher-core/src/bundled_versions.rs::
    playwright_mcp_launch_args`` (the Rust builder + GUI catalog); both build
    from the same ``[npx.playwright_mcp]`` row, and each side's unit test pins
    this exact element order.
    """
    p = pin or load_pin()
    return ["-y", p.spec, "--browser", p.browser]


def registration_entry(pin: Optional[Pin] = None) -> dict:
    """The full ``~/.claude.json`` ``mcpServers.playwright`` entry."""
    return {
        "type": "stdio",
        "command": "npx",
        "args": registration_args(pin),
        "env": {},
    }


def is_current_entry(entry: Any, pin: Optional[Pin] = None) -> bool:
    """True iff ``entry`` is exactly the registration this checkout writes."""
    if not isinstance(entry, dict) or entry.get("command") != "npx":
        return False
    return entry.get("args") == registration_args(pin)


def is_legacy_entry(entry: Any) -> bool:
    """True iff ``entry`` is the pre-pin ``npx -y @playwright/mcp@latest``."""
    if not isinstance(entry, dict) or entry.get("command") != "npx":
        return False
    return entry.get("args") == list(LEGACY_ARGS)


def is_vco_shaped_entry(entry: Any, pin: Optional[Pin] = None) -> bool:
    """VCO's own playwright fingerprint: the current pinned registration OR
    the legacy ``@latest`` one. A user's own playwright (another tag, no
    ``-y``, another package, an absolute path) matches neither."""
    return is_current_entry(entry, pin) or is_legacy_entry(entry)


# ---------------------------------------------------------------------------
# Where things live (npx cache, browser registry)
# ---------------------------------------------------------------------------


def _env(environ: Optional[Mapping[str, str]], key: str) -> str:
    env = os.environ if environ is None else environ
    return str(env.get(key) or "")


def npm_cache_dir(
    environ: Optional[Mapping[str, str]] = None,
    home: Optional[Path] = None,
    platform: Optional[str] = None,
) -> Path:
    """npm's cache directory (the parent of ``_npx``).

    ``npm_config_cache`` wins (npm reads it case-insensitively); otherwise
    npm's own defaults: ``%LOCALAPPDATA%\\npm-cache`` on Windows, ``~/.npm``
    elsewhere.
    """
    override = _env(environ, "npm_config_cache") or _env(environ, "NPM_CONFIG_CACHE")
    if override:
        return Path(override)
    plat = platform or sys.platform
    base_home = home or Path.home()
    if plat.startswith("win"):
        local = _env(environ, "LOCALAPPDATA")
        return (Path(local) if local else base_home / "AppData" / "Local") / "npm-cache"
    return base_home / ".npm"


def browsers_registry_dir(
    environ: Optional[Mapping[str, str]] = None,
    home: Optional[Path] = None,
    platform: Optional[str] = None,
    node_modules: Optional[Path] = None,
) -> Optional[Path]:
    """Where playwright-core keeps downloaded browsers.

    Mirrors ``registryDirectory`` / ``computeDefaultCacheDirectory`` in
    playwright-core: ``PLAYWRIGHT_BROWSERS_PATH`` (``"0"`` = the package-local
    ``.local-browsers``), else ``$XDG_CACHE_HOME`` or ``~/.cache`` on Linux,
    ``~/Library/Caches`` on macOS, ``%LOCALAPPDATA%`` on Windows — each with
    ``ms-playwright`` appended. ``None`` when ``"0"`` is set but the package
    location is unknown.
    """
    override = _env(environ, "PLAYWRIGHT_BROWSERS_PATH")
    if override == "0":
        if node_modules is None:
            return None
        return node_modules / "playwright-core" / ".local-browsers"
    if override:
        return Path(override)
    plat = platform or sys.platform
    base_home = home or Path.home()
    if plat.startswith("win"):
        local = _env(environ, "LOCALAPPDATA")
        base = Path(local) if local else base_home / "AppData" / "Local"
    elif plat == "darwin":
        base = base_home / "Library" / "Caches"
    else:
        xdg = _env(environ, "XDG_CACHE_HOME")
        base = Path(xdg) if xdg else base_home / ".cache"
    return base / "ms-playwright"


@dataclass(frozen=True)
class CachedPackage:
    """The pinned ``@playwright/mcp`` as found in the npx cache."""

    #: The ``node_modules`` directory holding ``@playwright/mcp`` and its
    #: ``playwright-core``.
    node_modules: Path
    #: Exact ``playwright-core`` version the package depends on (or None).
    core_version: Optional[str]
    #: Whether the package's ``cli.js`` implements ``install-browser``.
    has_install_browser: bool


def _read_json(path: Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def cached_package(
    pin: Pin,
    *,
    npm_cache: Optional[Path] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> Optional[CachedPackage]:
    """Find the pinned package in ``<npm cache>/_npx/*/node_modules``.

    npx keys its cache directories by a hash of the requested spec, so the
    directory name is not derivable; any directory whose
    ``@playwright/mcp/package.json`` carries EXACTLY the pinned version is the
    one the MCP will run. Read-only; ``None`` when none does.
    """
    root = (npm_cache or npm_cache_dir(environ)) / "_npx"
    try:
        candidates = sorted(root.iterdir())
    except OSError:
        return None
    pkg_parts = pin.package.split("/")
    for cand in candidates:
        node_modules = cand / "node_modules"
        pkg_dir = node_modules.joinpath(*pkg_parts)
        manifest = _read_json(pkg_dir / "package.json")
        if not manifest or manifest.get("version") != pin.version:
            continue
        deps = manifest.get("dependencies") or {}
        core = deps.get("playwright-core") if isinstance(deps, dict) else None
        try:
            cli_text = (pkg_dir / "cli.js").read_text(encoding="utf-8")
        except OSError:
            cli_text = ""
        return CachedPackage(
            node_modules=node_modules,
            core_version=str(core) if core else None,
            has_install_browser="install-browser" in cli_text,
        )
    return None


def expected_revisions(node_modules: Path, browser: str) -> tuple[str, ...]:
    """The browser revision(s) the pinned package's playwright-core requests.

    Read from ``playwright-core/browsers.json`` — the file playwright-core's
    own registry reads. ``revisionOverrides`` (per-OS revisions, used today by
    webkit only) are included as acceptable alternatives. Empty when the file
    or the browser's row is unreadable.
    """
    data = _read_json(node_modules / "playwright-core" / "browsers.json")
    if not data:
        return ()
    for row in data.get("browsers") or []:
        if isinstance(row, dict) and row.get("name") == browser and row.get("revision"):
            revs = [str(row["revision"])]
            overrides = row.get("revisionOverrides") or {}
            if isinstance(overrides, dict):
                revs.extend(str(v) for v in overrides.values() if v)
            return tuple(dict.fromkeys(revs))
    return ()


# ---------------------------------------------------------------------------
# The install command (one plan, shared by the pre-cache and the remedy)
# ---------------------------------------------------------------------------


def browser_install_argv(
    pin: Pin, cached: Optional[CachedPackage], npx: str = "npx"
) -> list[str]:
    """Install the browser through the PINNED package's own playwright-core.

    Default: ``npx -y <pin> install-browser <browser>`` — ``@playwright/mcp``'s
    ``cli.js`` maps ``install-browser`` onto playwright-core's ``install``.
    If the cached pinned package demonstrably lacks that subcommand (a future
    pin), fall back to ``npx -y playwright@<its exact playwright-core
    version> install <browser>`` — same core, same revision.
    """
    if cached is not None and not cached.has_install_browser and cached.core_version:
        return [npx, "-y", f"playwright@{cached.core_version}", "install", pin.browser]
    return [npx, "-y", pin.spec, "install-browser", pin.browser]


def render_argv(argv: Sequence[str]) -> str:
    """Render an argv as one pasteable line (the shell the user has)."""
    from vco_lib import remedy_shell

    return " ".join(remedy_shell.quote(a) for a in argv)


# ---------------------------------------------------------------------------
# The reading
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BrowserState:
    state: str
    pin: Pin
    reason: str
    revisions: tuple[str, ...] = ()
    #: The browser directory checked (the first candidate when missing).
    path: Optional[Path] = None
    repair_argv: list[str] = field(default_factory=list)

    @property
    def repair_command(self) -> str:
        return render_argv(self.repair_argv) if self.repair_argv else ""


def browser_state(
    pin: Optional[Pin] = None,
    *,
    environ: Optional[Mapping[str, str]] = None,
    home: Optional[Path] = None,
    platform: Optional[str] = None,
    npm_cache: Optional[Path] = None,
) -> BrowserState:
    """Is the browser the pinned MCP will request on disk?

    ``present`` — ``<registry>/<browser>-<rev>/INSTALLATION_COMPLETE`` exists
    for a revision the pinned package's playwright-core names (the marker is
    what playwright-core itself writes after a complete download).
    ``missing`` — the revision is known and no such directory is complete.
    ``unknown`` — the pinned package is not in the npx cache (its revision
    cannot be read) or the registry location is undeterminable. Positive
    evidence only: ``unknown`` is never rendered as healthy or as broken.

    The ONE exception it lets through is :func:`load_pin`'s ``KeyError`` — a
    broken pin table is a broken install, not a browser state.
    """
    p = pin or load_pin()
    try:
        cached = cached_package(p, npm_cache=npm_cache, environ=environ)
        repair = browser_install_argv(p, cached)
        if cached is None:
            return BrowserState(
                STATE_UNKNOWN, p,
                f"{p.spec} is not in the npx cache, so the {p.browser} revision it "
                "needs cannot be read",
                repair_argv=repair,
            )
        revisions = expected_revisions(cached.node_modules, p.browser)
        if not revisions:
            return BrowserState(
                STATE_UNKNOWN, p,
                f"{p.spec}'s playwright-core/browsers.json names no {p.browser} revision",
                repair_argv=repair,
            )
        registry = browsers_registry_dir(environ, home, platform, cached.node_modules)
        if registry is None:
            return BrowserState(
                STATE_UNKNOWN, p, "PLAYWRIGHT_BROWSERS_PATH=0 but the package "
                "location is unknown", revisions=revisions, repair_argv=repair,
            )
        for rev in revisions:
            cand = registry / f"{p.browser}-{rev}"
            if (cand / "INSTALLATION_COMPLETE").is_file():
                return BrowserState(
                    STATE_PRESENT, p,
                    f"{p.browser} revision {rev} for {p.spec} is installed at {cand}",
                    revisions=revisions, path=cand, repair_argv=repair,
                )
        first = registry / f"{p.browser}-{revisions[0]}"
        return BrowserState(
            STATE_MISSING, p,
            f"{p.spec} launches {p.browser} revision {revisions[0]}, and "
            f"{first} is not a complete install",
            revisions=revisions, path=first, repair_argv=repair,
        )
    except Exception as exc:  # noqa: BLE001 — could not look is not a verdict
        return BrowserState(STATE_UNKNOWN, p, f"browser check raised: {exc}")


# ---------------------------------------------------------------------------
# The deferral
# ---------------------------------------------------------------------------


def browser_missing_entry(state: BrowserState, detected: str = ""):
    """The ``playwright_browser_missing`` entry — ONE builder, shared by the
    install-time pre-cache and the doctor probe."""
    from vco_lib.deferral_report import DeferralEntry

    command = state.repair_command or render_argv(browser_install_argv(state.pin, None))
    return DeferralEntry(
        condition_id=CID_BROWSER_MISSING,
        title="The Playwright MCP's browser is not installed",
        detected=detected or state.reason,
        why_deferred=(
            f"VCO registers the `playwright` MCP as `npx {' '.join(registration_args(state.pin))}`, "
            f"which launches Playwright's own {state.pin.browser} at the revision its "
            "bundled playwright-core names. That browser is a download of roughly "
            "170 MB, so VCO fetches it only during an install/update (opt out with "
            f"`{SKIP_ENV}=1`) — and that download did not leave it on disk. Until it "
            "is there, every browser tool of the MCP fails at launch. Run the "
            "command below (it uses the pinned package's own installer, so the "
            "revision matches); the entry clears itself on the next run that finds "
            "the browser installed."
        ),
        command_to_apply=command,
        severity="warning",
        disposition="action_required",
        kg_node_refs=["docs/features/02-mcps-and-agents.md"],
    )


# ---------------------------------------------------------------------------
# The install-time pre-cache (install.py calls this through a thin shim)
# ---------------------------------------------------------------------------


Runner = Callable[..., Any]
LogEvent = Callable[..., None]


def precache(
    *,
    npx_path: Optional[str],
    runner: Runner,
    log_event: LogEvent,
    report: Any = None,
    environ: Optional[Mapping[str, str]] = None,
    out: Callable[[str], None] = print,
    pin: Optional[Pin] = None,
    state_fn: Callable[..., BrowserState] = browser_state,
) -> str:
    """Pre-cache the pinned MCP package and the browser it will request.

    Steps (each soft-fails; nothing here may abort an install):

    1. ``npx -y <pin> --version`` — puts the exact pinned package in the npx
       cache (the one the registration will run).
    2. :func:`browser_install_argv` — the pinned package's own installer.
    3. Re-read :func:`browser_state`. When the browser is not positively
       present, add ``playwright_browser_missing`` to ``report`` (the run's
       deferral report) carrying the exact repair command.

    ``runner`` is install.py's ``_run_logged_subprocess`` (dot-cycle +
    stderr tail); it is called with ``on_failure="return"`` and may raise
    ``subprocess.TimeoutExpired`` / ``OSError``. Returns a short outcome word
    (``skipped`` / ``no-npx`` / ``ok`` / ``failed``) for tests and logs.
    """
    import subprocess

    env = os.environ if environ is None else environ
    if env.get(SKIP_ENV) == "1":
        out(f"SKIPPED ({SKIP_ENV}=1)")
        log_event("playwright", "skip", f"{SKIP_ENV}=1 in env")
        return "skipped"
    if not npx_path:
        out("SKIPPED (npx not found)")
        out("  Node.js / npx not detected. The Playwright MCP is registered")
        out("  as `npx …`, so it CANNOT SPAWN until npx exists — Claude Code")
        out("  will show it as 'Failed to connect'. Install Node.js 18+")
        out("  (https://nodejs.org), then re-run this install.")
        log_event("playwright", "skip",
                  "npx not on PATH and not adjacent to npm — playwright MCP "
                  "cannot spawn (doctor phase emits npx_missing_mcp_unspawnable)")
        return "no-npx"

    p = pin or load_pin()
    out(f"({p.spec} + {p.browser}; may take a few minutes, ~170 MB)")

    def _defer(why: str) -> str:
        state = state_fn(p)
        if state.state == STATE_PRESENT:
            # An earlier run already left the right revision: nothing is owed.
            log_event("playwright", "ok", f"{why}; but {state.reason}")
            return "ok"
        if report is not None:
            report.add_entry(browser_missing_entry(state, detected=f"{why}. {state.reason}."))
        log_event("playwright", "warn", f"{why} — {state.reason}")
        return "failed"

    try:
        result = runner(
            [npx_path, "-y", p.spec, "--version"],
            step="playwright", phase_label="playwright-mcp-fetch",
            timeout=PACKAGE_FETCH_TIMEOUT_S,
            on_failure="return",
            fail_message=f"  WARN: fetching {p.spec} failed.",
        )
        if result.returncode != 0:
            return _defer(f"`npx -y {p.spec} --version` exited {result.returncode}")
    except subprocess.TimeoutExpired:
        out(f"  WARN: fetching {p.spec} timed out after {PACKAGE_FETCH_TIMEOUT_S}s.")
        return _defer(f"fetching {p.spec} timed out ({PACKAGE_FETCH_TIMEOUT_S}s)")
    except OSError as exc:
        out(f"  WARN: fetching {p.spec} failed: {exc}")
        return _defer(f"fetching {p.spec} failed: {exc}")

    cached = cached_package(p, environ=environ)
    argv = browser_install_argv(p, cached, npx=npx_path)
    try:
        result = runner(
            argv,
            step="playwright", phase_label="playwright-browser-install",
            timeout=BROWSER_INSTALL_TIMEOUT_S,
            on_failure="return",
            fail_message=f"  WARN: {p.browser} install exited non-zero.",
        )
        if result.returncode != 0:
            return _defer(f"`{shlex.join(argv[1:])}` exited {result.returncode}")
    except subprocess.TimeoutExpired:
        out(f"  WARN: {p.browser} install timed out after {BROWSER_INSTALL_TIMEOUT_S}s.")
        return _defer(f"{p.browser} install timed out ({BROWSER_INSTALL_TIMEOUT_S}s)")
    except OSError as exc:
        out(f"  WARN: {p.browser} install failed: {exc}")
        return _defer(f"{p.browser} install failed: {exc}")

    state = state_fn(p)
    if state.state == STATE_PRESENT:
        out(f"[playwright] {p.browser} cached OK ({state.path}).")
        log_event("playwright", "ok", state.reason)
        return "ok"
    return _defer(f"`{shlex.join(argv[1:])}` exited 0")


# ---------------------------------------------------------------------------
# The doctor probe + registry clear probe (doctor.py registers thin shims)
# ---------------------------------------------------------------------------


def _is_install_root(folder: Path) -> bool:
    return (Path(folder) / "vco_lib" / "__init__.py").is_file()


def probe_browser(folder: Path, res, ctx: dict) -> list:
    """Doctor probe ``playwright_browser`` (install roots only).

    Reads the REGISTERED entry first: no ``playwright`` entry, or a user's
    own, means VCO launches nothing and there is nothing to check. The legacy
    ``@latest`` entry is reported ``unknown`` (it uses the system-Chrome
    channel the pre-cache cannot serve; the next update rewrites it). The
    current entry gets the :func:`browser_state` reading: ``present`` is an
    OK finding CARRYING the cid (so the same reading clears an earlier
    entry), ``missing`` a deferred problem, ``unknown`` an unknown.
    """
    from vco_lib import doctor as d

    if not _is_install_root(Path(folder)):
        return []
    entry = (res.resolve_mcp_entries() or {}).get("playwright")
    if entry is None:
        return []
    if is_legacy_entry(entry):
        return [d.Finding(
            probe="playwright_browser",
            status=d.STATUS_UNKNOWN,
            summary=("the registered playwright MCP is the pre-pin "
                     "`@playwright/mcp@latest` (system-Chrome channel); "
                     "`python install.py --update` rewrites it to the pinned "
                     "Chromium launch"),
            command="python install.py --update",
        )]
    pin = load_pin()
    if not is_current_entry(entry, pin):
        return []
    environ = res.environ() if res.environ is not None else os.environ
    if environ.get(SKIP_ENV) == "1":
        return [d.Finding(
            probe="playwright_browser",
            status=d.STATUS_UNKNOWN,
            summary=(f"not checked: {SKIP_ENV}=1 (the eager browser download is "
                     "opted out); the MCP's browser tools need it installed"),
            command=render_argv(browser_install_argv(pin, None)),
        )]
    state = res.resolve_playwright_browser_state()
    detail = {
        "state": state.state, "spec": state.pin.spec, "browser": state.pin.browser,
        "revisions": list(state.revisions),
        "path": str(state.path) if state.path else "",
        "repair_argv": list(state.repair_argv),
    }
    if state.state == STATE_PRESENT:
        return [d.Finding(
            probe="playwright_browser", status=d.STATUS_OK,
            summary=state.reason, condition_id=CID_BROWSER_MISSING, detail=detail,
        )]
    if state.state == STATE_MISSING:
        return [d.Finding(
            probe="playwright_browser", status=d.STATUS_PROBLEM,
            summary=state.reason, fix=d.FIX_DEFER,
            condition_id=CID_BROWSER_MISSING, command=state.repair_command,
            detail=detail,
        )]
    return [d.Finding(
        probe="playwright_browser", status=d.STATUS_UNKNOWN,
        summary=state.reason, command=state.repair_command, detail=detail,
    )]


def doctor_entry(finding):
    """The doctor's ``playwright_browser_missing`` entry (one builder)."""
    pin = load_pin()
    state = BrowserState(
        STATE_MISSING, pin, finding.summary,
        revisions=tuple(finding.detail.get("revisions") or ()),
        repair_argv=list(finding.detail.get("repair_argv") or []),
    )
    return browser_missing_entry(state, detected=finding.summary)


def browser_still_missing(folder: Path) -> Optional[bool]:
    """Registry clear probe ``playwright_browser_still_missing``.

    True while the reading is ``missing``; False once ``present``; None when
    it is ``unknown`` or ``folder`` is not an install root (an unlookable
    state never reads as resolved).
    """
    if not _is_install_root(Path(folder)):
        return None
    try:
        state = browser_state()
    except Exception:  # noqa: BLE001 — a probe defect is not a verdict
        return None
    if state.state == STATE_PRESENT:
        return False
    if state.state == STATE_MISSING:
        return True
    return None
