# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Live tests for scripts/check-dist-freshness.py (v0.2.54 G-1.5).

The v0.2.53 tag shipped all three launcher/dist/<os>/ sidecar sets with
``source_hash fd215c7a`` (built at v0.2.52) while the live launcher
subtree hashed ``449e6cc6``. Both install-time freshness readers
(post-install-launcher.sh + first-install.bat) therefore rejected the
bundled binary on every fresh clone and fell through to the GitHub
download — which was itself broken on Windows (zip-vs-exe filter).
Release CI's Gate 3 only checked that launcher/dist/ CHANGED since the
previous tag, so the stale-vs-HEAD condition sailed through.

These tests run the real script as a subprocess against synthetic git
repos that reproduce both states:

* fresh repo  -> exit 0 in both modes;
* v0.2.53-style stale repo -> exit 1 in --mode strict (naming each OS
  dir), exit 0 with warnings in --mode warn.

Would-have-caught check: ``test_stale_sidecars_fail_strict`` builds a
repo in exactly the v0.2.53 state (committed sidecar hash != live
launcher-subtree hash) and asserts the strict gate goes red.

A parity test also pins the subtree path list against
scripts/build-bundled-launcher.sh so the writer and the gate can never
silently drift apart.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check-dist-freshness.py"
BUILD_SCRIPT = REPO_ROOT / "scripts" / "build-bundled-launcher.sh"


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, timeout=30
    )
    assert out.returncode == 0, f"git {args} failed: {out.stderr}"
    return out.stdout.strip()


def _make_repo(tmp_path: Path) -> Path:
    """Minimal git repo with the launcher subtree files the hash covers."""
    repo = tmp_path / "repo"
    (repo / "launcher" / "src-tauri" / "src").mkdir(parents=True)
    (repo / "launcher" / "src").mkdir(parents=True)
    (repo / "launcher" / "src-tauri" / "src" / "main.rs").write_text(
        'fn main() { println!("v1"); }\n'
    )
    (repo / "launcher" / "src" / "app.ts").write_text("export const v = 1;\n")
    (repo / "launcher" / "src-tauri" / "Cargo.toml").write_text(
        '[package]\nname = "vct-launcher"\nversion = "0.0.1"\n'
    )
    (repo / "launcher" / "src-tauri" / "Cargo.lock").write_text("# lock v1\n")
    (repo / "launcher" / "package.json").write_text('{"name": "launcher"}\n')
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@test.invalid")
    _git(repo, "config", "user.name", "test")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "initial launcher source")
    return repo


def _live_hash(repo: Path) -> str:
    ls = _git(
        repo,
        "ls-tree",
        "HEAD",
        "launcher/src-tauri/src/",
        "launcher/src/",
        "launcher/src-tauri/Cargo.toml",
        "launcher/src-tauri/Cargo.lock",
        "launcher/package.json",
    )
    out = subprocess.run(
        ["git", "hash-object", "--stdin"],
        cwd=repo,
        input=ls + "\n" if not ls.endswith("\n") else ls,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert out.returncode == 0
    return out.stdout.strip()


def _write_sidecars(repo: Path, source_hash: str, version: str = "0.2.52") -> None:
    for os_dir, binary in [
        ("linux-x64", "vct-launcher"),
        ("macos-arm64", "vct-launcher"),
        ("windows-x64", "vct-launcher.exe"),
    ]:
        d = repo / "launcher" / "dist" / os_dir
        d.mkdir(parents=True, exist_ok=True)
        (d / binary).write_bytes(b"fake-binary")
        (d / f"{binary}.metadata.json").write_text(
            json.dumps(
                {
                    "source_hash": source_hash,
                    "built_at": "2026-06-09T17:31:30Z",
                    "launcher_version": version,
                    "binary_name": binary,
                }
            )
        )


def _run_gate(repo: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--repo-root", str(repo), *extra],
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_fresh_sidecars_pass_both_modes(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _write_sidecars(repo, _live_hash(repo), version="0.2.54")
    for mode in ("warn", "strict"):
        out = _run_gate(repo, "--mode", mode)
        assert out.returncode == 0, (
            f"mode={mode}: rc={out.returncode}\n{out.stdout}\n{out.stderr}"
        )
        assert "OK: all bundled-binary sidecars match live source" in out.stdout


def test_stale_sidecars_fail_strict(tmp_path: Path) -> None:
    """Exact v0.2.53 foot-gun: sidecars built from an older source tree.

    Sidecar hash deliberately differs from the live launcher-subtree
    hash — what release CI Gate 3 could not see. Strict mode must exit 1
    and name every stale OS dir.
    """
    repo = _make_repo(tmp_path)
    stale_hash = "fd215c7a3ec5f179f1ee9d3d694c201cc7a58043"  # real v0.2.52 value
    assert stale_hash != _live_hash(repo)
    _write_sidecars(repo, stale_hash)

    out = _run_gate(repo, "--mode", "strict")
    assert out.returncode == 1, f"rc={out.returncode}\n{out.stdout}\n{out.stderr}"
    for os_dir in ("linux-x64", "macos-arm64", "windows-x64"):
        assert os_dir in out.stdout, f"stale report missing {os_dir}:\n{out.stdout}"
    assert "DIST-FRESHNESS GATE FAIL" in out.stdout


def test_stale_sidecars_warn_mode_exits_zero(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _write_sidecars(repo, "0" * 40)
    out = _run_gate(repo, "--mode", "warn")
    assert out.returncode == 0, f"rc={out.returncode}\n{out.stdout}\n{out.stderr}"
    assert "WARNING" in out.stdout
    out_gh = _run_gate(repo, "--mode", "warn", "--github")
    assert out_gh.returncode == 0
    assert "::warning::" in out_gh.stdout


def test_cross_os_inconsistency_detected(tmp_path: Path) -> None:
    """Three OS builds from different refs (v0.2.49 failure class)."""
    repo = _make_repo(tmp_path)
    live = _live_hash(repo)
    _write_sidecars(repo, live)
    # Flip one OS to a different hash — even one that ISN'T stale-vs-live
    # on the others must trip the consistency check.
    win_meta = (
        repo / "launcher" / "dist" / "windows-x64" / "vct-launcher.exe.metadata.json"
    )
    meta = json.loads(win_meta.read_text())
    meta["source_hash"] = "1" * 40
    win_meta.write_text(json.dumps(meta))

    out = _run_gate(repo, "--mode", "strict")
    assert out.returncode == 1
    assert "cross-OS inconsistency" in out.stdout


def test_missing_sidecars_is_env_error(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    (repo / "launcher" / "dist" / "linux-x64").mkdir(parents=True)
    out = _run_gate(repo, "--mode", "strict")
    assert out.returncode == 2
    assert "no *.metadata.json sidecars" in out.stdout


def test_dist_dir_override(tmp_path: Path) -> None:
    """--dist-dir validates an artifact staging tree (commit-dist-binaries)."""
    repo = _make_repo(tmp_path)
    staging = tmp_path / "_dist-artifacts" / "linux-x64"
    staging.mkdir(parents=True)
    (staging / "vct-launcher.metadata.json").write_text(
        json.dumps({"source_hash": _live_hash(repo), "built_at": "x", "launcher_version": "y"})
    )
    out = _run_gate(
        repo, "--mode", "strict", "--dist-dir", str(tmp_path / "_dist-artifacts")
    )
    assert out.returncode == 0, f"{out.stdout}\n{out.stderr}"


def test_subtree_paths_parity_with_build_script() -> None:
    """The gate's hash inputs must match build-bundled-launcher.sh exactly.

    The writer computes SOURCE_HASH via `git ls-tree HEAD <paths>`; the
    gate recomputes it. If the path lists drift, every comparison becomes
    meaningless (always-stale or always-fresh). Pin them together.
    """
    build_src = BUILD_SCRIPT.read_text(encoding="utf-8")
    m = re.search(r"git ls-tree HEAD ((?:\S+ )+?)2>/dev/null", build_src)
    assert m, "cannot locate SOURCE_HASH ls-tree invocation in build script"
    writer_paths = m.group(1).split()

    gate_src = SCRIPT.read_text(encoding="utf-8")
    m2 = re.search(r"LAUNCHER_SUBTREE_PATHS = \[(.*?)\]", gate_src, re.DOTALL)
    assert m2, "cannot locate LAUNCHER_SUBTREE_PATHS in gate script"
    gate_paths = re.findall(r'"([^"]+)"', m2.group(1))

    assert gate_paths == writer_paths, (
        f"gate paths {gate_paths} != build-script paths {writer_paths} — "
        "update both sides together"
    )
