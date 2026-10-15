# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Live regression tests for scripts/lib/download-launcher-asset.ps1.

v0.2.54 G-1.5 (Wave 0 follow-up). first-install.bat's inline downloader
filtered GitHub-release assets with ``$_.name.EndsWith('.exe')``, but
release.yml has shipped ONLY ``vibecoded-orchestrator-<ver>-windows-x64.zip``
since the 2026-05-10 uniform-zip packaging change. Verified against the
live v0.2.53 release (gh API, 2026-06-11): 3 x ``.zip`` + 3 x
``.zip.sha256``, zero ``.exe`` assets. The filter therefore ALWAYS hit
NO_ASSET and every Windows first-run silently fell through to the
15-30 min source build.

Per the live-test discipline (memory note: argv-shape tests miss live CLI
parser rejections), these tests run the REAL PowerShell helper end to end
against a localhost HTTP server serving a synthetic release payload whose
asset list mirrors the actual v0.2.53 release. They are skipped when
pwsh/powershell is not on PATH (Linux dev boxes without PowerShell);
GitHub's ubuntu/windows runners both ship pwsh, so CI always runs them.

Would-have-caught check: ``test_zip_only_release_downloads_and_extracts``
feeds the exact v0.2.53-shaped asset list. Under the pre-fix
``EndsWith('.exe')`` filter the helper would exit 2 (NO_ASSET); the test
asserts exit 0 + the binary landing on disk.
"""

from __future__ import annotations

import hashlib
import http.server
import io
import json
import shutil
import subprocess
import sys
import threading
import zipfile
from pathlib import Path

import pytest
from tests.common.pwsh import PWSH_BASE_FLAGS

REPO_ROOT = Path(__file__).resolve().parent.parent
HELPER = REPO_ROOT / "scripts" / "lib" / "download-launcher-asset.ps1"
FIRST_INSTALL_BAT = REPO_ROOT / "first-install.bat"

LAUNCHER_BYTES = b"MZ-fake-vct-launcher-payload-" + b"L" * 4096
HUB_BYTES = b"MZ-fake-vct-hub-payload-" + b"H" * 2048
UPDATER_BYTES = b"MZ-fake-vct-updater-payload-" + b"U" * 1024


def _pwsh() -> str | None:
    return shutil.which("pwsh") or shutil.which("powershell")


def _make_release_zip(inner_root: str, include_launcher: bool = True) -> bytes:
    """Build an in-memory zip mirroring the real release archive layout:
    binaries nested under vibecoded-orchestrator-<ver>-windows-x64/."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        if include_launcher:
            z.writestr(f"{inner_root}/vct-launcher.exe", LAUNCHER_BYTES)
        z.writestr(f"{inner_root}/vct-hub.exe", HUB_BYTES)
        z.writestr(f"{inner_root}/vct-updater.exe", UPDATER_BYTES)
        z.writestr(f"{inner_root}/README.md", "release archive fixture\n")
    return buf.getvalue()


class _FixtureServer:
    """Localhost HTTP server serving /release.json + named asset blobs."""

    def __init__(self, blobs: dict[str, bytes]):
        self.blobs = blobs
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 (http.server contract)
                name = self.path.lstrip("/")
                body = outer.blobs.get(name)
                if body is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):  # noqa: A002 — silence test output
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()

    def url(self, name: str) -> str:
        return f"http://127.0.0.1:{self.port}/{name}"


def _release_json(server: _FixtureServer, asset_names: list[str]) -> bytes:
    assets = [
        {"name": n, "browser_download_url": server.url(n)} for n in asset_names
    ]
    return json.dumps({"tag_name": "v0.2.53", "assets": assets}).encode()


def _sidecar(blob: bytes, name: str) -> bytes:
    """A sha256sum-shaped sidecar for *blob*, as release.yml publishes it."""
    return f"{hashlib.sha256(blob).hexdigest()}  {name}\n".encode()


def _run_helper(dest_dir: Path, api_url: str, *,
                python_exe: str | None = sys.executable) -> subprocess.CompletedProcess:
    extra = ["-PythonExe", python_exe] if python_exe else []
    return subprocess.run(
        [
            _pwsh() or "pwsh",
            *PWSH_BASE_FLAGS,
            "-NonInteractive",
            "-File",
            str(HELPER),
            "-DestDir",
            str(dest_dir),
            "-ApiUrl",
            api_url,
            # Fixture binaries are KB-sized; the production default (10MB)
            # would false-trip TOO_SMALL.
            "-MinSizeBytes",
            "1024",
            *extra,
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )


pwsh_required = pytest.mark.skipif(
    _pwsh() is None,
    reason="pwsh/powershell not on PATH — covered by ubuntu + windows CI runners",
)


@pwsh_required
def test_zip_only_release_downloads_and_extracts(tmp_path: Path) -> None:
    """v0.2.53-shaped release (zip + sha256 only, NO .exe asset).

    This is the exact payload shape that made the pre-fix
    EndsWith('.exe') filter return NO_ASSET (exit 2) on every Windows
    first-run. The fixed helper must pick the windows .zip (NOT the
    .zip.sha256 sidecar), extract, and land all three binaries.
    """
    inner = "vibecoded-orchestrator-0.2.53-windows-x64"
    zip_bytes = _make_release_zip(inner)
    blobs: dict[str, bytes] = {
        # sha256 sidecars listed FIRST to prove the picker does not
        # grab them (they match '*windows*' but not EndsWith('.zip')).
        "vibecoded-orchestrator-0.2.53-windows-x64.zip.sha256": _sidecar(
            zip_bytes, f"{inner}.zip"),
        "vibecoded-orchestrator-0.2.53-linux-x64.zip": b"not-windows",
        "vibecoded-orchestrator-0.2.53-linux-x64.zip.sha256": b"deadbeef\n",
        "vibecoded-orchestrator-0.2.53-macos-arm64.zip": b"not-windows",
        "vibecoded-orchestrator-0.2.53-macos-arm64.zip.sha256": b"deadbeef\n",
        f"{inner}.zip": zip_bytes,
    }
    with _FixtureServer(blobs) as srv:
        # Asset order mirrors the live v0.2.53 release listing, sha256
        # entries interleaved before the windows zip.
        srv.blobs["release.json"] = _release_json(
            srv,
            [
                "vibecoded-orchestrator-0.2.53-linux-x64.zip",
                "vibecoded-orchestrator-0.2.53-linux-x64.zip.sha256",
                "vibecoded-orchestrator-0.2.53-macos-arm64.zip",
                "vibecoded-orchestrator-0.2.53-macos-arm64.zip.sha256",
                "vibecoded-orchestrator-0.2.53-windows-x64.zip.sha256",
                f"{inner}.zip",
            ],
        )
        dest = tmp_path / "dest"
        out = _run_helper(dest, srv.url("release.json"))

    assert out.returncode == 0, (
        f"helper failed: rc={out.returncode}\nstdout={out.stdout}\nstderr={out.stderr}"
    )
    assert "NO_ASSET" not in out.stdout
    assert (dest / "vct-launcher.exe").read_bytes() == LAUNCHER_BYTES
    # hub + updater ride along in the same archive (v0.2.21+ / v0.2.52+).
    assert (dest / "vct-hub.exe").read_bytes() == HUB_BYTES
    assert (dest / "vct-updater.exe").read_bytes() == UPDATER_BYTES


@pwsh_required
def test_no_windows_asset_exits_2(tmp_path: Path) -> None:
    """Release with no windows asset at all -> NO_ASSET, exit 2."""
    with _FixtureServer({}) as srv:
        srv.blobs["release.json"] = _release_json(
            srv, ["vibecoded-orchestrator-0.2.53-linux-x64.zip"]
        )
        out = _run_helper(tmp_path / "dest", srv.url("release.json"))
    assert out.returncode == 2, f"stdout={out.stdout}\nstderr={out.stderr}"
    assert "NO_ASSET" in out.stdout


@pwsh_required
def test_legacy_exe_asset_fallback(tmp_path: Path) -> None:
    """Pre-2026-05-10 packaging (bare .exe asset) still works."""
    blobs = {"vct-launcher-windows-x64.exe": LAUNCHER_BYTES,
             "vct-launcher-windows-x64.exe.sha256": _sidecar(
                 LAUNCHER_BYTES, "vct-launcher-windows-x64.exe")}
    with _FixtureServer(blobs) as srv:
        srv.blobs["release.json"] = _release_json(
            srv, ["vct-launcher-windows-x64.exe", "vct-launcher-windows-x64.exe.sha256"]
        )
        dest = tmp_path / "dest"
        out = _run_helper(dest, srv.url("release.json"))
    assert out.returncode == 0, f"stdout={out.stdout}\nstderr={out.stderr}"
    assert (dest / "vct-launcher.exe").read_bytes() == LAUNCHER_BYTES


@pwsh_required
def test_zip_without_launcher_exits_4(tmp_path: Path) -> None:
    """Zip downloaded + extracted but no vct-launcher.exe inside -> exit 4."""
    inner = "vibecoded-orchestrator-0.2.53-windows-x64"
    zip_bytes = _make_release_zip(inner, include_launcher=False)
    blobs = {f"{inner}.zip": zip_bytes,
             f"{inner}.zip.sha256": _sidecar(zip_bytes, f"{inner}.zip")}
    with _FixtureServer(blobs) as srv:
        srv.blobs["release.json"] = _release_json(srv, [f"{inner}.zip", f"{inner}.zip.sha256"])
        out = _run_helper(tmp_path / "dest", srv.url("release.json"))
    assert out.returncode == 4, f"stdout={out.stdout}\nstderr={out.stderr}"
    assert "NO_BINARY_IN_ZIP" in out.stdout


# ---------------------------------------------------------------------------
# R20 M1: the archive is verified against the release's .sha256 sidecar
# BEFORE anything is extracted or landed in -DestDir.
# ---------------------------------------------------------------------------


def _verify_case(tmp_path: Path, *, sidecar,
                 python_exe: str | None = sys.executable) -> tuple[subprocess.CompletedProcess, Path]:
    """``sidecar(zip_bytes, zip_name)`` builds the sidecar bytes (``None``:
    the release lists no sidecar). The zip is built ONCE — its entries
    carry the current time, so a second build could hash differently."""
    inner = "vibecoded-orchestrator-0.2.103-windows-x64"
    zip_bytes = _make_release_zip(inner)
    blobs = {f"{inner}.zip": zip_bytes}
    sidecar = sidecar(zip_bytes, f"{inner}.zip") if sidecar else None
    names = [f"{inner}.zip"]
    if sidecar is not None:
        blobs[f"{inner}.zip.sha256"] = sidecar
        names.append(f"{inner}.zip.sha256")
    dest = tmp_path / "dest"
    with _FixtureServer(blobs) as srv:
        srv.blobs["release.json"] = _release_json(srv, names)
        out = _run_helper(dest, srv.url("release.json"), python_exe=python_exe)
    return out, dest


@pwsh_required
def test_matching_checksum_lands_the_binary(tmp_path: Path) -> None:
    out, dest = _verify_case(tmp_path, sidecar=_sidecar)
    assert out.returncode == 0, f"stdout={out.stdout}\nstderr={out.stderr}"
    assert "Checksum verified" in out.stdout
    assert (dest / "vct-launcher.exe").read_bytes() == LAUNCHER_BYTES


@pwsh_required
def test_checksum_mismatch_exits_5_and_lands_nothing(tmp_path: Path) -> None:
    out, dest = _verify_case(
        tmp_path, sidecar=lambda _b, name: f"{'0' * 64}  {name}\n".encode())
    assert out.returncode == 5, f"stdout={out.stdout}\nstderr={out.stderr}"
    assert "CHECKSUM_MISMATCH" in out.stdout
    assert not (dest / "vct-launcher.exe").exists()
    assert not (dest / "vct-hub.exe").exists()


@pwsh_required
def test_missing_sidecar_exits_6_and_lands_nothing(tmp_path: Path) -> None:
    out, dest = _verify_case(tmp_path, sidecar=None)
    assert out.returncode == 6, f"stdout={out.stdout}\nstderr={out.stderr}"
    assert "UNVERIFIED" in out.stdout
    assert not (dest / "vct-launcher.exe").exists()


@pwsh_required
def test_no_verifier_interpreter_exits_6_and_lands_nothing(tmp_path: Path) -> None:
    """Without -PythonExe nothing can be verified, so nothing is run."""
    out, dest = _verify_case(tmp_path, sidecar=_sidecar, python_exe=None)
    assert out.returncode == 6, f"stdout={out.stdout}\nstderr={out.stderr}"
    assert not (dest / "vct-launcher.exe").exists()


# ---------------------------------------------------------------------------
# Static wiring assertions — run everywhere (no pwsh needed). These pin the
# .bat -> helper contract so a refactor can't silently reintroduce the
# exe-only inline filter.
# ---------------------------------------------------------------------------


def test_first_install_bat_uses_helper() -> None:
    src = FIRST_INSTALL_BAT.read_text(encoding="utf-8", errors="replace")
    assert "download-launcher-asset.ps1" in src, (
        "first-install.bat no longer invokes scripts\\lib\\download-launcher-asset.ps1"
    )


def test_first_install_bat_has_no_inline_exe_filter() -> None:
    """The buggy inline asset filter must not come back."""
    src = FIRST_INSTALL_BAT.read_text(encoding="utf-8", errors="replace")
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.upper().startswith("REM"):
            continue
        assert "EndsWith('.exe')" not in stripped, (
            "first-install.bat reintroduced the exe-only release-asset filter "
            f"(v0.2.53 NO_ASSET bug): {stripped!r}"
        )


def test_helper_prefers_zip_with_legacy_exe_fallback() -> None:
    # Scan code lines only — the header comment narrates the old .exe
    # filter and would false-position the search.
    code = "\n".join(
        line
        for line in HELPER.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )
    zip_pos = code.find("EndsWith('.zip')")
    exe_pos = code.find("EndsWith('.exe')")
    assert zip_pos != -1, "helper lost the .zip asset filter"
    assert exe_pos != -1, "helper lost the legacy .exe fallback"
    assert zip_pos < exe_pos, "helper must prefer .zip BEFORE the legacy .exe fallback"
