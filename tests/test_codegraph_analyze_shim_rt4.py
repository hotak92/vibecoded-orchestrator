# SPDX-License-Identifier: AGPL-3.0-or-later
"""RT-4 regression: the code-graph-analyze shim resilient interpreter discovery.

Background (RT-4): a project's INSTALLED `.claude/scripts/code-graph-analyze`
shim hardcoded an absolute legacy interpreter path
(`.../Claude/claude_mcp_servers/.venv/bin/python`). When that legacy venv was
removed, per-project code-graph builds died with exit 127 — the shim had no
fallback discovery. The fix mirrors the canonical binary-discovery order from
templates/hooks/_lib/resolve-vco-venv.sh (VCT_VENV -> VCT_INSTALL_ROOT ->
clone-relative), so a stale/removed default is always recoverable via the same
$VCT_VENV override the hooks honour.

These tests build a synthetic shim + a fake "venv" whose python stub imports a
stub `weaviate` module and echoes a marker, then assert:
  - with the default/clone venvs absent, the shim STILL resolves a working
    interpreter from $VCT_VENV (RT-4's remedy path);
  - the shim contains no hardcoded absolute legacy interpreter path;
  - the shim's candidate order matches the canonical resolver (source guard);
  - the .ps1 sibling mirrors the same tier order (parity + BOM).

Synthetic names only (ProjA / fake venvs) — no real project identity embedded.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[1]
_SHIM = _REPO_ROOT / "templates" / "scripts" / "code-graph-analyze"
_SHIM_PS1 = _REPO_ROOT / "templates" / "scripts" / "code-graph-analyze.ps1"


def _make_fake_venv(root: Path, marker: str, with_weaviate: bool = True) -> Path:
    """Create a fake venv dir whose bin/python is a shell stub that (a)
    succeeds `import weaviate` iff with_weaviate, and (b) prints ``marker``
    plus its args when run as the analyzer interpreter."""
    venv = root
    binp = venv / "bin"
    binp.mkdir(parents=True, exist_ok=True)
    py = binp / "python"
    # The stub interprets the two invocations the shim makes:
    #   1. python -c "import weaviate"   (dep validation gate)
    #   2. python <analyze_code_graph.py> <args...>   (the real run)
    weav_branch = "exit 0" if with_weaviate else "exit 1"
    py.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "-c" ]; then\n'
        f'  case "$2" in *weaviate*) {weav_branch} ;; *) exit 0 ;; esac\n'
        "fi\n"
        f'echo "{marker} $*"\n'
        "exit 0\n"
    )
    py.chmod(py.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return py


def _install_shim(dest_scripts: Path) -> Path:
    """Copy the shim + a trivial analyze_code_graph.py into a scripts dir."""
    dest_scripts.mkdir(parents=True, exist_ok=True)
    shim = dest_scripts / "code-graph-analyze"
    shutil.copy2(_SHIM, shim)
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
    # A placeholder analyzer so the shim has a target to invoke (only the
    # stub python ever "runs" it — content is irrelevant).
    (dest_scripts / "analyze_code_graph.py").write_text("# placeholder\n")
    return shim


def _clean_env() -> dict:
    env = dict(os.environ)
    # DISCIPLINE: any test spawning a venv-resolving path MUST clear the
    # ambient VCT_VENV so a real machine value can't hijack the assertion.
    env.pop("VCT_VENV", None)
    env.pop("VCT_INSTALL_ROOT", None)
    return env


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_shim_resolves_vct_venv_when_default_absent(tmp_path: Path) -> None:
    """The core RT-4 case: the clone-relative / install-root venvs do NOT
    exist, but $VCT_VENV points at a working interpreter → the shim resolves
    it instead of dying with 'no interpreter'."""
    # Lay the shim inside a fake USER PROJECT (.claude/scripts) whose
    # clone-relative ../../.venv deliberately does NOT exist.
    proj = tmp_path / "ProjA"
    scripts = proj / ".claude" / "scripts"
    shim = _install_shim(scripts)

    good = _make_fake_venv(tmp_path / "good_venv", marker="RESOLVED_VCT_VENV")

    env = _clean_env()
    env["VCT_VENV"] = str((tmp_path / "good_venv"))

    out = subprocess.run(
        ["bash", str(shim), ".", "--project", "ProjA"],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert out.returncode == 0, f"shim failed: {out.stderr}\n{out.stdout}"
    assert "RESOLVED_VCT_VENV" in out.stdout, (
        "shim must resolve the interpreter from $VCT_VENV when the default "
        f"clone/install venvs are absent. stdout={out.stdout!r}"
    )
    assert good.exists()


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_shim_accepts_vct_venv_as_direct_interpreter(tmp_path: Path) -> None:
    """$VCT_VENV may point straight at the python binary (not the venv dir),
    mirroring resolve-vco-venv.sh tier 1."""
    proj = tmp_path / "ProjA"
    scripts = proj / ".claude" / "scripts"
    shim = _install_shim(scripts)
    py = _make_fake_venv(tmp_path / "good_venv", marker="RESOLVED_DIRECT")

    env = _clean_env()
    env["VCT_VENV"] = str(py)  # the interpreter itself

    out = subprocess.run(
        ["bash", str(shim), "."],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert out.returncode == 0, out.stderr
    assert "RESOLVED_DIRECT" in out.stdout, out.stdout


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_shim_skips_venv_without_analyzer_deps(tmp_path: Path) -> None:
    """A venv lacking weaviate-client must NOT be selected — the dep gate
    protects against activating the user's own project venv. With only a
    deps-less VCT_VENV, the shim falls through to system python3 (which
    also lacks it), NOT the wrong venv."""
    proj = tmp_path / "ProjA"
    scripts = proj / ".claude" / "scripts"
    shim = _install_shim(scripts)
    _make_fake_venv(tmp_path / "bad_venv", marker="WRONG_VENV", with_weaviate=False)

    env = _clean_env()
    env["VCT_VENV"] = str(tmp_path / "bad_venv")

    out = subprocess.run(
        ["bash", str(shim), "."],
        env=env, capture_output=True, text=True, timeout=30,
    )
    # It must not have used the deps-less venv's stub marker.
    assert "WRONG_VENV" not in out.stdout, (
        "shim selected a venv without weaviate-client — dep gate regressed"
    )


def _shim_code_lines() -> list:
    """Return only executable code lines (strip full-line `#` comments) so a
    hardcode guard doesn't trip on explanatory prose that names the legacy
    path as the very bug it documents."""
    out = []
    for line in _SHIM.read_text(encoding="utf-8").splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        out.append(line)
    return out


def test_shim_has_no_hardcoded_legacy_interpreter_path() -> None:
    """The exact RT-4 failure: a hardcoded absolute legacy venv python path.

    Checked against CODE lines only — the header comment legitimately names
    the legacy path when documenting the bug it fixes."""
    code = _shim_code_lines()
    for line in code:
        assert "Claude/claude_mcp_servers/.venv/bin/python" not in line, (
            f"shim hardcodes the removed legacy interpreter path (RT-4): {line!r}"
        )
        # No absolute /home/... interpreter path in executable code either.
        assert "/home/" not in line, f"hardcoded home path in shim code: {line!r}"


def test_shim_candidate_order_matches_canonical_tiers() -> None:
    """Source guard: the .sh shim probes VCT_VENV first, then
    VCT_INSTALL_ROOT, then clone-relative — the canonical order."""
    body = _SHIM.read_text(encoding="utf-8")
    i_vct_venv = body.find('"${VCT_VENV:-}"')
    i_install = body.find('"${VCT_INSTALL_ROOT:-}/.venv"')
    i_clone = body.find('"$SCRIPT_DIR/../../.venv"')
    assert -1 < i_vct_venv < i_install < i_clone, (
        "candidate order must be VCT_VENV -> VCT_INSTALL_ROOT -> clone-relative"
    )


def test_ps1_sibling_mirrors_tiers_and_has_bom() -> None:
    """Multi-OS parity: the .ps1 sibling mirrors the same tier order and
    keeps a UTF-8 BOM (Windows PowerShell encoding discipline)."""
    raw = _SHIM_PS1.read_bytes()
    assert raw[:3] == b"\xef\xbb\xbf", ".ps1 must retain its UTF-8 BOM"
    text = raw.decode("utf-8-sig")
    i_vct_venv = text.find("$env:VCT_VENV")
    i_install = text.find("$env:VCT_INSTALL_ROOT")
    i_proj = text.find("$ProjectRoot")
    # First mentions in the candidate array must be VCT_VENV < VCT_INSTALL_ROOT.
    assert -1 < i_vct_venv < i_install, (
        ".ps1 must probe $env:VCT_VENV before $env:VCT_INSTALL_ROOT"
    )
    assert i_proj != -1, ".ps1 must retain the clone-relative $ProjectRoot tier"
    assert "import weaviate" in text, (
        ".ps1 must validate analyzer deps like the .sh sibling"
    )
