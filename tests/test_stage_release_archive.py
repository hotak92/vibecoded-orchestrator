# SPDX-License-Identifier: AGPL-3.0-or-later
"""v0.2.109 — the release-zip layout contract.

The v0.2.108 zips were staged from a hand-written copy list in release.yml:
no ``vco_lib/``, ``scripts/``, ``launcher/``, ``migrations/`` or
``pyproject.toml``, and the binaries at the zip root — ``install.py`` died on
its first import on every OS. ``scripts/stage-release-archive.sh`` is now the
one home for the layout (``git archive`` of the ref + explicit exclusions +
binaries at ``launcher/dist/<target>/``); ``scripts/smoke-install-from-zip.sh``
is what CI and the release run on the extracted zip.

These tests RUN both scripts. The red proofs build the v0.2.108 shape and
show the smoke rejects it.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from tests.common.pwsh import PWSH_BASE_FLAGS

REPO_ROOT = Path(__file__).resolve().parent.parent
STAGE = REPO_ROOT / "scripts" / "stage-release-archive.sh"
SMOKE = REPO_ROOT / "scripts" / "smoke-install-from-zip.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("git") is None,
    reason="needs bash + git",
)

TARGETS = {"linux-x64": "", "windows-x64": ".exe", "macos-arm64": ""}
BINARIES = ("vct-launcher", "vct-hub", "vct-updater")

# What an install from the extracted tree needs (install.py imports vco_lib;
# validate_source_repo() wants install.py + first-install.sh on every OS;
# the shims source scripts/lib/; VCThelpers/ + uninstall.* run at install /
# uninstall time).
REQUIRED = (
    "install.py", "first-install.sh", "first-install.command",
    "first-install.bat", "install.sh", "install.ps1", "start-launcher.sh",
    "start-launcher.command", "start-launcher.bat", "uninstall.sh",
    "uninstall.bat", "pyproject.toml", "requirements.txt",
    "vco_lib/__init__.py", "VCThelpers/__init__.py", "scripts/lib/x.sh",
    "migrations/001.sql", "claude_mcp_servers/s.py", "templates/t.md",
    "infrastructure/docker-compose.yml", "tools/t.sh",
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"},
    )


def _write(path: Path, text: str = "x\n", mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if mode is not None:
        path.chmod(mode)


@pytest.fixture()
def fake_repo(tmp_path: Path) -> Path:
    """A small git repo with the shape of the real tree (all three targets'
    committed binaries, tests/, .github/, a Windows-only vendored binary)."""
    repo = tmp_path / "repo"
    for rel in REQUIRED:
        _write(repo / rel)
    for rel in ("first-install.sh", "first-install.command", "install.sh",
                "start-launcher.sh", "start-launcher.command", "uninstall.sh"):
        (repo / rel).chmod(0o755)
    _write(repo / "launcher/src-tauri/Cargo.toml", '[package]\nname = "x"\nversion = "9.8.7"\n')
    _write(repo / "vct-config.example.toml", "# example\n")
    _write(repo / "docs/MIGRATION-9.8.7.md", "# notes\n")
    _write(repo / "tests/test_x.py")
    _write(repo / ".github/workflows/ci.yml")
    _write(repo / "vendor/lean-ctx/VERSION.md")
    _write(repo / "vendor/lean-ctx/windows-x64/lean-ctx.exe", "MZ")
    _write(repo / "launcher/dist/README.md")
    for target, exe in TARGETS.items():
        for b in BINARIES:
            _write(repo / f"launcher/dist/{target}/{b}{exe}", f"committed {target} {b}\n", 0o755)
            _write(repo / f"launcher/dist/{target}/{b}{exe}.metadata.json", "{}\n")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed")
    return repo


def _stage(repo: Path, out: Path, target: str, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(STAGE), "--repo", str(repo), "--target", target,
         "--out-dir", str(out), *extra],
        capture_output=True, text=True, timeout=300, check=False,
    )


def _smoke(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SMOKE), *args], capture_output=True, text=True, timeout=300,
        check=False, env={**os.environ, **(env or {})},
    )


@pytest.mark.parametrize("target", sorted(TARGETS))
def test_layout_contract(fake_repo: Path, tmp_path: Path, target: str) -> None:
    exe = TARGETS[target]
    res = _stage(fake_repo, tmp_path / "out", target, "--version", "9.8.7")
    assert res.returncode == 0, res.stderr
    root = tmp_path / "out" / f"vibecoded-orchestrator-9.8.7-{target}"  # default name
    for rel in REQUIRED:
        assert (root / rel).is_file(), f"{target}: {rel} missing"
    dist = root / "launcher" / "dist" / target
    for b in BINARIES:
        assert (dist / f"{b}{exe}").is_file()
        assert not (root / f"{b}{exe}").exists(), "binary must not sit at the archive root"
    if not exe:
        assert os.access(dist / "vct-launcher", os.X_OK)
        assert os.access(root / "first-install.command", os.X_OK)
    # Exclusions: dev-only + the other targets' binaries + other-OS vendor.
    assert not (root / "tests").exists()
    assert not (root / ".github").exists()
    for other in set(TARGETS) - {target}:
        assert not (root / "launcher" / "dist" / other).exists()
    assert (root / "vendor/lean-ctx/windows-x64").exists() == (target == "windows-x64")
    assert (root / "vendor/lean-ctx/VERSION.md").is_file()
    # The launcher reads vct-config.toml from its own folder.
    assert (dist / "vct-config.toml").is_file()
    assert (root / "MIGRATION-9.8.7.md").is_file()


def test_built_binaries_overlay_the_committed_ones(fake_repo: Path, tmp_path: Path) -> None:
    bin_dir = tmp_path / "built"
    for b in BINARIES:
        _write(bin_dir / b, f"fresh {b}\n", 0o755)
    _write(bin_dir / "_release" / "vct-launcher", "renamed copy\n")
    cli = tmp_path / "vct-cli"
    _write(cli, "cli\n", 0o755)
    res = _stage(fake_repo, tmp_path / "out", "linux-x64", "--bin-dir", str(bin_dir),
                 "--cli", str(cli), "--archive-base", "vco-test")
    assert res.returncode == 0, res.stderr
    dist = tmp_path / "out" / "vco-test" / "launcher" / "dist" / "linux-x64"
    assert (dist / "vct-launcher").read_text() == "fresh vct-launcher\n"
    assert (dist / "vct-cli").read_text() == "cli\n"
    assert not (dist / "_release").exists()
    # A sidecar the build did not rewrite keeps the committed copy.
    assert (dist / "vct-launcher.metadata.json").is_file()


def test_only_tracked_content_ships(fake_repo: Path, tmp_path: Path) -> None:
    _write(fake_repo / "vco_lib" / "untracked_scratch.py")
    _write(fake_repo / ".venv" / "pyvenv.cfg")
    res = _stage(fake_repo, tmp_path / "out", "linux-x64", "--archive-base", "b")
    assert res.returncode == 0, res.stderr
    root = tmp_path / "out" / "b"
    assert not (root / "vco_lib" / "untracked_scratch.py").exists()
    assert not (root / ".venv").exists()


def test_missing_required_binary_fails(fake_repo: Path, tmp_path: Path) -> None:
    bin_dir = tmp_path / "built"
    _write(bin_dir / "vct-launcher", "x", 0o755)
    _write(bin_dir / "vct-hub", "x", 0o755)
    _git(fake_repo, "rm", "-q", "--cached", "launcher/dist/linux-x64/vct-updater")
    _git(fake_repo, "commit", "-q", "-m", "drop updater")
    res = _stage(fake_repo, tmp_path / "out", "linux-x64", "--bin-dir", str(bin_dir),
                 "--archive-base", "b")
    assert res.returncode == 1
    assert "vct-updater" in res.stderr


def test_unknown_target_is_a_usage_error(fake_repo: Path, tmp_path: Path) -> None:
    res = _stage(fake_repo, tmp_path / "out", "linux-arm64")
    assert res.returncode == 2


def test_zip_holds_one_top_folder_with_the_binaries_inside(fake_repo: Path, tmp_path: Path) -> None:
    if shutil.which("zip") is None and shutil.which("pwsh") is None:
        pytest.skip("needs zip or pwsh")
    res = _stage(fake_repo, tmp_path / "out", "linux-x64", "--archive-base", "vco-9-linux-x64", "--zip")
    assert res.returncode == 0, res.stderr
    with zipfile.ZipFile(tmp_path / "out" / "vco-9-linux-x64.zip") as z:
        names = z.namelist()
        assert {n.split("/", 1)[0] for n in names} == {"vco-9-linux-x64"}
        member = "vco-9-linux-x64/launcher/dist/linux-x64/vct-launcher"
        assert member in names
        assert "vco-9-linux-x64/vco_lib/__init__.py" in names
        assert "vco-9-linux-x64/vct-launcher" not in names
        if shutil.which("zip"):
            mode = z.getinfo(member).external_attr >> 16
            assert mode & stat.S_IXUSR, "exec bit must survive the zip"


# ── smoke-install-from-zip.sh ──────────────────────────────────────────────


def _zip_dir(src_root: Path, zip_path: Path) -> None:
    with zipfile.ZipFile(zip_path, "w") as z:
        for p in sorted(src_root.rglob("*")):
            if p.is_file():
                z.write(p, f"{src_root.name}/{p.relative_to(src_root).as_posix()}")


def _old_layout(tmp_path: Path) -> Path:
    """The v0.2.108 shape: the hand-picked subset, binaries at the root."""
    root = tmp_path / "old" / "vibecoded-orchestrator-0.2.108-linux-x64"
    for rel in ("install.py", "first-install.sh", "README.md", "templates/t.md",
                "claude_mcp_servers/s.py", "tools/t.sh", "infrastructure/c.yml",
                "vct-config.toml", "vct-cli"):
        _write(root / rel)
    for b in ("vct-launcher", "vct-hub"):
        _write(root / b, "bin", 0o755)
    return root


def test_smoke_extract_refuses_more_than_one_top_folder(tmp_path: Path) -> None:
    z = tmp_path / "flat.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("install.py", "")
        zf.writestr("vct-launcher", "")
    res = _smoke("extract", str(z), str(tmp_path / "x"))
    assert res.returncode == 1
    assert "exactly one top folder" in res.stderr


def test_smoke_probe_rejects_the_v0_2_108_layout(tmp_path: Path) -> None:
    """RED proof: the old zip shape fails the probe CI and the release run."""
    old = _old_layout(tmp_path)
    z = tmp_path / "old.zip"
    _zip_dir(old, z)
    ext = _smoke("extract", str(z), str(tmp_path / "x"))
    assert ext.returncode == 0, ext.stderr
    root = ext.stdout.strip()
    res = _smoke("probe", root, "linux-x64")
    assert res.returncode == 1
    for rel in ("vco_lib/__init__.py", "scripts/lib", "pyproject.toml", "migrations",
                "launcher/dist/linux-x64/vct-launcher"):
        assert f"lacks {rel}" in res.stdout, res.stdout


def test_smoke_probe_passes_on_the_real_staged_tree(tmp_path: Path, fake_claude_cli: object) -> None:
    """GREEN proof on the REAL tree: stage this repo's HEAD, zip-free extract
    path, and run the real ``install.py --bootstrap --json`` from it."""
    out = tmp_path / "out"
    res = _stage(REPO_ROOT, out, "linux-x64", "--archive-base", "real")
    assert res.returncode == 0, res.stderr
    root = out / "real"
    probe = _smoke("probe", str(root), "linux-x64",
                   env={"PYTHON": sys.executable, "WEAVIATE_URL": "http://127.0.0.1:9"})
    assert probe.returncode == 0, probe.stdout + probe.stderr
    env = json.loads((root / "bootstrap-pre.json").read_text(encoding="utf-8"))
    assert env["paths"]["launcher_binary_exists"] is True
    assert env["paths"]["launcher_binary"].endswith("launcher/dist/linux-x64/vct-launcher")


def test_smoke_probe_fails_when_the_bootstrap_cannot_import(tmp_path: Path) -> None:
    """Layout present but the bootstrap dies (the v0.2.108 symptom): the
    probe fails and shows the interpreter's error."""
    root = tmp_path / "r"
    for rel in ("first-install.sh", "pyproject.toml", "vco_lib/__init__.py",
                "scripts/lib/x.sh", "migrations/1.sql"):
        _write(root / rel)
    for b in BINARIES:
        _write(root / f"launcher/dist/linux-x64/{b}", "x", 0o755)
    _write(root / "vco_lib/__init__.py", "")
    _write(root / "install.py", "import vco_lib.does_not_exist\n")
    res = _smoke("probe", str(root), "linux-x64", env={"PYTHON": sys.executable})
    assert res.returncode == 1
    assert "ModuleNotFoundError" in res.stderr


# ── first-install bootstrap probe no longer swallows its error ─────────────


def _stub_python(bin_dir: Path) -> None:
    """Interpreter stub: passes the version gate, FAILS the bootstrap probe
    with an import error on stderr, succeeds on the real install call."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    body = (
        "#!/bin/sh\n"
        'if [ "$1" = "-c" ]; then printf "%s" "3.12"; exit 0; fi\n'
        'if [ "$2" = "--bootstrap" ]; then\n'
        "  echo \"ModuleNotFoundError: No module named 'vco_lib'\" >&2; exit 3\n"
        "fi\n"
        'echo "install called: $*"; exit 0\n'
    )
    for name in ("python3.13", "python3.12", "python3.11", "python3", "python", "py"):
        p = bin_dir / name
        p.write_text(body, encoding="utf-8")
        p.chmod(0o755)


@pytest.mark.parametrize("shim", ["first-install.sh", "first-install.command", "install.ps1"])
def test_failed_bootstrap_probe_is_surfaced_not_fatal(tmp_path: Path, shim: str) -> None:
    if shim.endswith(".ps1") and shutil.which("pwsh") is None:
        pytest.skip("pwsh not installed")
    repo = tmp_path / "repo"
    repo.mkdir()
    shutil.copy2(REPO_ROOT / shim, repo / shim)
    bin_dir = tmp_path / "bin"
    _stub_python(bin_dir)
    home = tmp_path / "home"
    home.mkdir()
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(home), "CI": "1",
           "VCT_NON_INTERACTIVE": "1"}
    if shim.endswith(".ps1"):
        cmd = [shutil.which("pwsh") or "pwsh", *PWSH_BASE_FLAGS, "-File", shim,
               "-NonInteractive", "-NoContainers"]
    else:
        cmd = ["bash", shim, "--no-auto-launch"]
    res = subprocess.run(cmd, cwd=repo, env=env, capture_output=True, text=True,
                         timeout=180, check=False)
    out = res.stdout + res.stderr
    assert res.returncode == 0, out  # optional probe: the install still ran
    assert "install called: install.py" in out
    assert "bootstrap probe (install.py --bootstrap --json) exited 3" in out
    assert "No module named 'vco_lib'" in out
    err_files = list(repo.rglob("bootstrap-prepass.stderr"))
    assert err_files, "the probe's stderr must be kept on disk"
