# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Pinned-npm install + drift-detection core (v0.2.77 Part 7a-bis, task 3).

Extracted from ``install.py`` to break the back-edge where
``vco_lib.cli.verify`` imported ``install._install_pinned_npm`` (a vco_lib
module reaching UP into the top-level ``install`` script). The logic now
lives here; ``install.py`` keeps thin name-stable wrappers
(``_install_pinned_npm`` / ``_check_npm_pin_drift`` /
``_record_npm_pin_drift_deferral`` / ``_resolve_pinned_package`` and the
private helpers) so the existing test surface — which patches
``install._NPM_PATH``, ``install._BUNDLED_VERSIONS_AUDIT_LOG``,
``subprocess.run`` (stdlib, global), and ``bundled_versions.
load_bundled_versions`` — keeps working unchanged.

Dependency injection: the pure functions here take explicit
``npm_path`` / ``audit_log_path`` / ``project_root`` / ``log_event`` params
rather than reading ``install``-module globals, so vco_lib never imports
``install``. The wrapper in ``install.py`` reads its module-level cache
values at call time and threads them in — that indirection is what keeps
``mock.patch.object(install, "_NPM_PATH", ...)`` effective.

``subprocess.run`` / ``json`` / ``shutil`` are imported at module top so the
global ``mock.patch.object(subprocess, "run", ...)`` used across the tests
intercepts calls made from THIS module too.
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from vco_lib import bundled_versions as _bundled_versions
from vco_lib.deferral_report import DeferralEntry, DeferralReport

# A no-op log callback so callers that don't care about forensic logging
# (tests, ad-hoc use) don't have to pass one.
LogEvent = Callable[..., None]


def _noop_log(step: str, phase: str, detail: str = "", *, data: Any = None) -> None:  # noqa: ARG001
    return None


def _utc_iso_now() -> str:
    """ISO-8601 UTC timestamp with second precision, Z suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def append_bundled_versions_audit(
    record: dict,
    *,
    audit_log_path: Path,
) -> None:
    """Append one JSONL line to the bundled-versions audit log.

    Never raises — log failures must not break the install. Directory is
    created if missing.
    """
    try:
        audit_log_path.parent.mkdir(parents=True, exist_ok=True)
        record_full = dict(record)
        record_full.setdefault("timestamp", _utc_iso_now())
        with audit_log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record_full, ensure_ascii=True) + "\n")
            f.flush()
    except Exception:
        # NEVER let a log failure break the install.
        pass


def resolve_pinned_package(package_key: str) -> dict[str, str]:
    """Return the pinned spec for ``package_key`` from the manifest.

    Raises:
        KeyError: ``[npm.<key>]`` missing from the manifest. The KeyError
            message names the manifest path so the user can correct it.
    """
    versions = _bundled_versions.load_bundled_versions()
    # The manifest loader is typed dict[str, dict[str, str]] but the [npm]
    # block's values are per-package spec MAPPINGS (package/version/shasum),
    # so treat the resolved spec as Any for the field probing below.
    npm_block: Any = versions.get("npm", {})
    if package_key not in npm_block:
        manifest_path = _bundled_versions.manifest_path()
        raise KeyError(
            f"package key {package_key!r} not present in [npm] section "
            f"of {manifest_path}. Add the entry there, then re-run."
        )
    spec: Any = npm_block[package_key]
    for required in ("package", "version", "shasum"):
        if required not in spec:
            raise KeyError(
                f"[npm.{package_key}] in {_bundled_versions.manifest_path()} "
                f"is missing required field {required!r} (have: "
                f"{sorted(spec.keys())})."
            )
    return spec


def query_installed_npm_version(
    package: str,
    *,
    npm_path: Optional[str],
    timeout: int = 60,
) -> Optional[str]:
    """Return the version of a globally-installed npm package, or None.

    Uses ``npm ls -g --json --depth=0 <package>`` and parses
    ``dependencies[package].version``. Returns None when npm is absent, the
    package is not installed globally, or the subprocess errors / times out.
    """
    if npm_path is None:
        return None
    try:
        result = subprocess.run(
            [npm_path, "ls", "-g", "--json", "--depth=0", package],
            capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if not result.stdout.strip():
        return None
    try:
        parsed = json.loads(result.stdout)
    except (ValueError, TypeError):
        return None
    deps = parsed.get("dependencies") or {}
    entry = deps.get(package) or {}
    return (entry.get("version") or "").strip() or None


def installed_npm_integrity(
    package: str,
    *,
    npm_path: Optional[str],
    timeout: int = 60,
) -> Optional[str]:
    """Best-effort: return the SHA-1 (``dist.shasum``) of the LOCALLY
    INSTALLED copy of ``package`` if npm exposes it on this version.

    Older npm clients don't populate ``_shasum``/``_integrity`` in
    ``ls --json`` output; on those we return None and the caller logs a WARN
    and skips strict integrity comparison.
    """
    if npm_path is None:
        return None
    try:
        result = subprocess.run(
            [npm_path, "ls", "-g", "--json", "--depth=0", package],
            capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if not result.stdout.strip():
        return None
    try:
        parsed = json.loads(result.stdout)
    except (ValueError, TypeError):
        return None
    deps = parsed.get("dependencies") or {}
    entry = deps.get(package) or {}
    return entry.get("_shasum") or None


def _truthy_env(env_value: Optional[str]) -> bool:
    """Accept "1", "true", "yes", "on" (case-insensitive); anything else
    (including unset / empty) is falsy."""
    if env_value is None:
        return False
    return env_value.strip().lower() in ("1", "true", "yes", "on")


def _read_vendored_package_json(local_dir: Path) -> Optional[dict]:
    """Read ``<local_dir>/package.json`` and return the parsed dict, or None
    if the file is missing or unreadable / malformed."""
    pkg_json = local_dir / "package.json"
    try:
        text = pkg_json.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def _read_vendored_package_name(local_dir: Path) -> Optional[str]:
    """Return the ``name`` field of the vendored package.json, or None."""
    pkg = _read_vendored_package_json(local_dir)
    if pkg is None:
        return None
    name = pkg.get("name")
    return name if isinstance(name, str) and name else None


def _read_vendored_package_version(local_dir: Path) -> Optional[str]:
    """Return the ``version`` field of the vendored package.json, or None."""
    pkg = _read_vendored_package_json(local_dir)
    if pkg is None:
        return None
    ver = pkg.get("version")
    return ver if isinstance(ver, str) and ver else None


def install_pinned_npm(
    package_key: str,
    *,
    npm_path: Optional[str],
    audit_log_path: Path,
    project_root: Path,
    log_event: Optional[LogEvent] = None,
    skip_env_getter: Callable[[str], Optional[str]] | None = None,
    skip_env_var: Optional[str] = None,
    timeout: int = 300,
) -> bool:
    """Install one npm package at the exact version pinned in
    ``bundled_mcp_versions.toml``.

    Dependency-injected params (vs the old ``install``-module globals):
        npm_path: resolved ``npm`` executable path, or None if absent.
        audit_log_path: JSONL audit-log destination.
        project_root: anchor for ``file:`` pin resolution.
        log_event: forensic logger (``install._log_install_event`` shape).
        skip_env_getter: callable ``name -> value`` used to read the
            skip env var (defaults to ``os.environ.get``); injected so the
            wrapper controls env resolution.

    Returns True when the package is installed at the EXACT pinned version
    (whether we installed it now or it was already at the pin); False on
    skip / npm missing / version mismatch / integrity mismatch.
    """
    log = log_event or _noop_log
    if skip_env_getter is None:
        import os as _os
        skip_env_getter = _os.environ.get

    def _audit(record: dict) -> None:
        append_bundled_versions_audit(record, audit_log_path=audit_log_path)

    spec = resolve_pinned_package(package_key)
    package = spec["package"]
    version = spec["version"]
    expected_shasum = spec["shasum"]

    is_file_pin = package.startswith("file:")

    if is_file_pin:
        rel = package[len("file:"):]
        local_dir = (project_root / rel).resolve()
        print(f"[bundled-versions] Installing {package} (vendored, "
              f"label={version}, key={package_key}) ... ",
              end="", flush=True)
        log("bundled_versions", "start",
            f"pinning vendored {package} ({version})")
    else:
        local_dir = project_root  # unused for registry pins
        print(f"[bundled-versions] Installing {package}@{version} "
              f"(key={package_key}) ... ", end="", flush=True)
        log("bundled_versions", "start", f"pinning {package}@{version}")

    if skip_env_var is not None and _truthy_env(skip_env_getter(skip_env_var)):
        print(f"SKIPPED ({skip_env_var}=truthy)")
        log("bundled_versions", "skip", f"{skip_env_var} set to truthy",
            data={"package": package, "version": version})
        _audit({
            "package": package, "package_key": package_key,
            "version": version, "shasum_expected": expected_shasum,
            "shasum_actual": None, "result": "skipped_env_var",
            "skip_env_var": skip_env_var,
        })
        return False

    if npm_path is None:
        print("SKIPPED (npm not found)")
        print("  Node.js / npm not detected. The MCP will lazy-install")
        print("  when first invoked. Install Node.js 18+ to pre-pin:")
        print("  https://nodejs.org")
        log("bundled_versions", "skip", "npm not on PATH",
            data={"package": package, "version": version})
        _audit({
            "package": package, "package_key": package_key,
            "version": version, "shasum_expected": expected_shasum,
            "shasum_actual": None, "result": "skipped_npm_missing",
        })
        return False

    if is_file_pin:
        vendor_pkg_name = _read_vendored_package_name(local_dir)
        if vendor_pkg_name is None:
            print(f"  WARN: vendored package.json missing or unreadable "
                  f"at {local_dir}.")
            log("bundled_versions", "warn",
                f"vendored package.json unreadable at {local_dir}",
                data={"package": package, "version": version})
            _audit({
                "package": package, "package_key": package_key,
                "version": version, "shasum_expected": expected_shasum,
                "shasum_actual": None, "result": "vendor_unreadable",
                "vendor_path": str(local_dir),
            })
            return False
        query_pkg_name = vendor_pkg_name
    else:
        query_pkg_name = package

    currently_installed = query_installed_npm_version(
        query_pkg_name, npm_path=npm_path,
    )
    if is_file_pin:
        vendor_version = _read_vendored_package_version(local_dir)
        already_pinned = (
            vendor_version is not None
            and currently_installed == vendor_version
        )
    else:
        already_pinned = currently_installed == version
    if already_pinned:
        print("OK (already pinned)")
        log("bundled_versions", "ok",
            f"{package} already at pinned {version}")
        _audit({
            "package": package, "package_key": package_key,
            "version": version, "shasum_expected": expected_shasum,
            "shasum_actual": installed_npm_integrity(
                query_pkg_name, npm_path=npm_path,
            ),
            "result": "already_pinned",
        })
        return True

    print(f"(install; was {currently_installed or 'absent'})")

    # CVE-1 (GHSA-5xrq-8626-4rwp): file-pinned installs get --omit=dev so
    # vendored forks don't ship devDependencies to every user.
    if is_file_pin:
        install_argv = [npm_path, "install", "-g", "--omit=dev", str(local_dir)]
    else:
        install_argv = [npm_path, "install", "-g", f"{package}@{version}"]

    try:
        result = subprocess.run(
            install_argv, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        print(f"  WARN: npm install timed out after {timeout}s.")
        log("bundled_versions", "warn",
            f"npm install timed out ({timeout}s)",
            data={"package": package, "version": version})
        _audit({
            "package": package, "package_key": package_key,
            "version": version, "shasum_expected": expected_shasum,
            "shasum_actual": None, "result": "install_timeout",
            "timeout_seconds": timeout,
        })
        return False
    except OSError as e:
        print(f"  WARN: npm install failed: {e}")
        log("bundled_versions", "warn", f"npm install failed: {e}",
            data={"package": package, "version": version})
        _audit({
            "package": package, "package_key": package_key,
            "version": version, "shasum_expected": expected_shasum,
            "shasum_actual": None, "result": "install_oserror",
            "error": str(e),
        })
        return False

    if result.returncode != 0:
        print("  WARN: npm install exited non-zero.")
        print(f"    stderr: {result.stderr.strip()[:200]}")
        log("bundled_versions", "warn", "npm install exited non-zero",
            data={"package": package, "version": version,
                  "returncode": result.returncode,
                  "stderr": result.stderr.strip()[:500]})
        _audit({
            "package": package, "package_key": package_key,
            "version": version, "shasum_expected": expected_shasum,
            "shasum_actual": None, "result": "install_nonzero",
            "returncode": result.returncode,
            "stderr": result.stderr.strip()[:500],
        })
        return False

    # Verify the installed version matches the pin.
    actual_version = query_installed_npm_version(
        query_pkg_name, npm_path=npm_path,
    )
    if is_file_pin:
        expected_version = _read_vendored_package_version(local_dir)
    else:
        expected_version = version
    if expected_version is not None and actual_version != expected_version:
        print(f"  WARN: post-install version mismatch — "
              f"expected {expected_version}, got {actual_version}.")
        log("bundled_versions", "warn", "post-install version mismatch",
            data={"package": package, "expected": expected_version,
                  "actual": actual_version})
        _audit({
            "package": package, "package_key": package_key,
            "version": version, "shasum_expected": expected_shasum,
            "shasum_actual": None, "result": "version_mismatch",
            "actual_version": actual_version,
        })
        return False

    # Best-effort integrity check. SKIPPED for file: pins.
    if is_file_pin:
        print(f"[bundled-versions] {package} OK (vendored, "
              f"label={version}; integrity = on-disk tree).")
        log("bundled_versions", "ok",
            f"{package} ({version}) installed from vendor")
        _audit({
            "package": package, "package_key": package_key,
            "version": version, "shasum_expected": expected_shasum,
            "shasum_actual": None, "result": "ok_vendored",
            "vendor_path": str(local_dir),
        })
        return True

    actual_shasum = installed_npm_integrity(query_pkg_name, npm_path=npm_path)
    if actual_shasum is None:
        print("  WARN: npm did not expose installed integrity; "
              "skipping strict shasum check.")
        log("bundled_versions", "warn",
            "integrity check skipped (npm did not expose _shasum)",
            data={"package": package, "version": version})
        _audit({
            "package": package, "package_key": package_key,
            "version": version, "shasum_expected": expected_shasum,
            "shasum_actual": None, "result": "integrity_check_skipped",
        })
        return True

    if actual_shasum != expected_shasum:
        print(f"  ERROR: integrity mismatch — expected "
              f"{expected_shasum}, got {actual_shasum}.")
        log("bundled_versions", "error", "integrity mismatch",
            data={"package": package, "expected": expected_shasum,
                  "actual": actual_shasum})
        _audit({
            "package": package, "package_key": package_key,
            "version": version, "shasum_expected": expected_shasum,
            "shasum_actual": actual_shasum, "result": "integrity_mismatch",
        })
        return False

    print(f"[bundled-versions] {package}@{version} OK "
          f"(shasum verified).")
    log("bundled_versions", "ok",
        f"{package}@{version} installed + verified")
    _audit({
        "package": package, "package_key": package_key,
        "version": version, "shasum_expected": expected_shasum,
        "shasum_actual": actual_shasum, "result": "ok",
    })
    return True


def check_npm_pin_drift(
    package_key: str,
    *,
    npm_path: Optional[str],
) -> tuple[bool, Optional[str]]:
    """Detect drift between the installed npm package and its pin.

    Returns ``(in_sync, drift_msg_or_None)``. ``in_sync`` is True when the
    installed version equals the pin (or npm is absent / package not
    installed / file: pin). ``drift_msg_or_None`` is a human-readable
    one-liner for the ``DeferralEntry.detected`` field.
    """
    spec = resolve_pinned_package(package_key)
    package = spec["package"]
    pinned = spec["version"]

    if npm_path is None:
        return (True, None)

    # For file: pins, the vendor IS the pin — no registry version to drift.
    if package.startswith("file:"):
        return (True, None)

    installed = query_installed_npm_version(package, npm_path=npm_path)
    if installed is None:
        return (True, None)

    if installed == pinned:
        return (True, None)

    return (
        False,
        f"installed {package} version {installed} differs from "
        f"bundled_mcp_versions.toml pin {pinned}. Run "
        f"`python install.py --update --force-pin-reset` to reinstall "
        f"the pinned version, or accept the drift by bumping the "
        f"manifest in your next VCO release.",
    )


def record_npm_pin_drift_deferral(
    package_key: str,
    drift_msg: str,
    report: DeferralReport,
) -> None:
    """Add a ``bundle_pin_drift_<key>`` entry to the deferral report.

    Caller (--update flow) decides when to write the report. The
    condition_id includes the package key so multiple drifts dedupe
    per-package.
    """
    report.add_entry(
        DeferralEntry(
            condition_id=f"bundle_pin_drift_{package_key}",
            title=f"Pinned npm package `{package_key}` drifted from manifest",
            detected=drift_msg,
            why_deferred=(
                "VCO does not auto-overwrite an existing global npm "
                "install — that would silently undo any deliberate "
                "out-of-band version the user installed. Resolution "
                "requires explicit consent."
            ),
            command_to_apply=(
                "python install.py --update --force-pin-reset"
            ),
            severity="warning",
            kg_node_refs=[
                "knowledge/concepts/install-flow-architectural-overhaul-2026-05-06.md",
            ],
        )
    )
