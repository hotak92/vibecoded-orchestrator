"""V47-E tests for _scan_foreign_compose_files (Gap E, v0.2.46 Part 2).

Covers:
  1. Empty project → empty list
  2. One foreign compose file → list contains it
  3. VCO-owned compose under infrastructure/ → excluded (empty list)
  4. VCO-owned compose under claude_mcp_servers/ → excluded (empty list)
  5. Mixed project (foreign + VCO-owned) → only foreign returned
  6. Depth limit: compose file 4+ levels deep is NOT detected
  7. Various filename patterns:
       docker-compose.yml, compose.yaml, podman-compose.dev.yml,
       docker-compose.override.yaml, compose.prod.yml, DOCKER-COMPOSE.YML
  8. Non-compose files are not returned
  9. Symlinks to compose files are excluded (is_file() follows links;
     but the helper checks is_symlink() before appending — tested
     separately on POSIX so the test skips gracefully on Windows if
     symlink creation requires privileges)
 10. Nested compose file at exactly depth 3 (boundary: IS detected)
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Load install.py without triggering its top-level argparse side effects.
# Pattern shared with other V46/V47 test files.
# ---------------------------------------------------------------------------
_INSTALL_PY = Path(__file__).resolve().parent.parent / "install.py"
_spec = importlib.util.spec_from_file_location("install_py_v47e", _INSTALL_PY)
install_py = importlib.util.module_from_spec(_spec)
sys.modules["install_py_v47e"] = install_py
_spec.loader.exec_module(install_py)

scan = install_py._scan_foreign_compose_files


# ---------------------------------------------------------------------------
# Test 1: empty project directory → empty list
# ---------------------------------------------------------------------------

def test_empty_project_returns_empty_list(tmp_path: Path) -> None:
    assert scan(tmp_path) == []


# ---------------------------------------------------------------------------
# Test 2: single foreign compose file → returned
# ---------------------------------------------------------------------------

def test_single_foreign_compose_file_detected(tmp_path: Path) -> None:
    compose = tmp_path / "docker-compose.yml"
    compose.write_text("version: '3'\n")
    result = scan(tmp_path)
    assert result == [Path("docker-compose.yml")]


# ---------------------------------------------------------------------------
# Test 3: VCO-owned compose under infrastructure/ → excluded
# ---------------------------------------------------------------------------

def test_vco_owned_infrastructure_compose_excluded(tmp_path: Path) -> None:
    infra = tmp_path / "infrastructure"
    infra.mkdir()
    vco_compose = infra / "docker-compose.yml"
    vco_compose.write_text("version: '3'\n")
    assert scan(tmp_path) == []


# ---------------------------------------------------------------------------
# Test 4: VCO-owned compose under claude_mcp_servers/ → excluded
# ---------------------------------------------------------------------------

def test_vco_owned_claude_mcp_servers_compose_excluded(tmp_path: Path) -> None:
    mcp_dir = tmp_path / "claude_mcp_servers"
    mcp_dir.mkdir()
    vco_compose = mcp_dir / "compose.yaml"
    vco_compose.write_text("services: {}\n")
    assert scan(tmp_path) == []


# ---------------------------------------------------------------------------
# Test 5: mixed project — foreign + VCO-owned → only foreign returned
# ---------------------------------------------------------------------------

def test_mixed_project_returns_only_foreign(tmp_path: Path) -> None:
    # VCO-owned (should be excluded)
    infra = tmp_path / "infrastructure"
    infra.mkdir()
    (infra / "docker-compose.yml").write_text("version: '3'\n")

    mcp = tmp_path / "claude_mcp_servers"
    mcp.mkdir()
    (mcp / "compose.yaml").write_text("services: {}\n")

    # Foreign (should be returned)
    foreign_root = tmp_path / "docker-compose.yml"
    foreign_root.write_text("version: '3'\n")
    subdir = tmp_path / "services"
    subdir.mkdir()
    foreign_sub = subdir / "compose.yml"
    foreign_sub.write_text("services: {}\n")

    result = scan(tmp_path)
    assert Path("docker-compose.yml") in result
    assert Path("services") / "compose.yml" in result
    # VCO-owned must NOT appear
    for p in result:
        assert not str(p).startswith("infrastructure")
        assert not str(p).startswith("claude_mcp_servers")


# ---------------------------------------------------------------------------
# Test 6: depth limit — file at depth 4 is NOT detected
# ---------------------------------------------------------------------------

def test_compose_file_at_depth_4_not_detected(tmp_path: Path) -> None:
    # depth 1 / 2 / 3 / 4
    d4 = tmp_path / "a" / "b" / "c" / "d"
    d4.mkdir(parents=True)
    (d4 / "docker-compose.yml").write_text("version: '3'\n")
    assert scan(tmp_path) == []


# ---------------------------------------------------------------------------
# Test 7: various filename patterns all detected
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("filename", [
    "docker-compose.yml",
    "docker-compose.yaml",
    "docker-compose.override.yml",
    "docker-compose.override.yaml",
    "compose.yml",
    "compose.yaml",
    "compose.prod.yml",
    "compose.prod.yaml",
    "podman-compose.yml",
    "podman-compose.yaml",
    "podman-compose.dev.yml",
    "podman-compose.dev.yaml",
])
def test_compose_filename_patterns_detected(tmp_path: Path, filename: str) -> None:
    (tmp_path / filename).write_text("services: {}\n")
    result = scan(tmp_path)
    assert Path(filename) in result, (
        f"Expected {filename!r} to be detected; got {result!r}"
    )


# ---------------------------------------------------------------------------
# Test 8: non-compose files are not returned
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("filename", [
    "README.md",
    "requirements.txt",
    ".env",
    "CLAUDE.md",
    "docker-compose",          # no suffix
    "compose.json",            # wrong suffix
    "my-docker-compose.yml",   # doesn't start with a known prefix
])
def test_non_compose_files_not_returned(tmp_path: Path, filename: str) -> None:
    (tmp_path / filename).write_text("content\n")
    assert scan(tmp_path) == [], (
        f"{filename!r} should not be returned by the scanner"
    )


# ---------------------------------------------------------------------------
# Test 9: symlinks to compose files are excluded (POSIX only)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Symlink creation may require admin on Windows; skip gracefully",
)
def test_symlink_to_compose_file_excluded(tmp_path: Path) -> None:
    real = tmp_path / "_real_compose.yml"
    real.write_text("version: '3'\n")
    link = tmp_path / "docker-compose.yml"
    link.symlink_to(real)
    # The symlink should NOT appear in results (is_symlink() guard)
    result = scan(tmp_path)
    assert Path("docker-compose.yml") not in result


# ---------------------------------------------------------------------------
# Test 10: compose file at exactly depth 3 IS detected (boundary check)
# ---------------------------------------------------------------------------

def test_compose_file_at_depth_3_detected(tmp_path: Path) -> None:
    # depth 1 / 2 / 3
    d3 = tmp_path / "a" / "b" / "c"
    d3.mkdir(parents=True)
    (d3 / "compose.yml").write_text("services: {}\n")
    result = scan(tmp_path)
    assert Path("a") / "b" / "c" / "compose.yml" in result


# ---------------------------------------------------------------------------
# Test 11: compose file at exactly depth 2 IS detected
# ---------------------------------------------------------------------------

def test_compose_file_at_depth_2_detected(tmp_path: Path) -> None:
    d2 = tmp_path / "apps" / "myapp"
    d2.mkdir(parents=True)
    (d2 / "docker-compose.yml").write_text("services: {}\n")
    result = scan(tmp_path)
    assert Path("apps") / "myapp" / "docker-compose.yml" in result


# ---------------------------------------------------------------------------
# Test 12: result is sorted (stable output for logs)
# ---------------------------------------------------------------------------

def test_result_is_sorted(tmp_path: Path) -> None:
    (tmp_path / "z-compose.yml").write_text("")
    (tmp_path / "a-compose.yml").write_text("")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "compose.yaml").write_text("")

    result = scan(tmp_path)
    assert result == sorted(result)
