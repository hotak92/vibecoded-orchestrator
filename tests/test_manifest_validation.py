"""V52-D.3: tests for `vco_lib.manifest_validation`.

Covers:
- Layer 1: required-key / JSON-shape validation
- Layer 2: runtime block pathology detection (parallels Rust's V52-D.1)
- Layer 3: install.scope coherence
- CLI subprocess interface (exit codes + JSON stdout)
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from vco_lib.manifest_validation import (
    ValidationResult,
    validate_install_scope_coherence,
    validate_manifest_dict,
    validate_manifest_file,
    validate_runtime_block,
)


# ─── Test fixtures ──────────────────────────────────────────────────────


def _good_manifest() -> dict:
    """A minimal valid manifest — every test that needs a valid base
    starts from this and mutates one field."""
    return {
        "id": "vct-rl-reranker",
        "version": "0.2.10",
        "install": {
            "method": "container_pull",
            "scope": "global",
            "container": {
                "image": "ghcr.io/hotak92/vct-rl-reranker",
                "tag_from_version": True,
            },
        },
        "runtime": {
            "type": "container",
            "command": "python",
            "args": ["-m", "rl_server.rl_server"],
            "container_name_template": "vct-rl-reranker",
        },
    }


def _bug_e_manifest() -> dict:
    """The empirical pre-v0.2.49 Bug E shape from vct-rl-reranker
    v0.2.9 — used as the canonical reject fixture."""
    m = _good_manifest()
    m["runtime"]["command"] = "podman"
    m["runtime"]["args"] = [
        "run",
        "--rm",
        "-p",
        "11450:11450",
        "{module_image}",
    ]
    m["install"]["scope"] = None
    return m


# ─── Layer 1: schema validation ─────────────────────────────────────────


def test_v52d3_good_manifest_passes() -> None:
    result = validate_manifest_dict(_good_manifest())
    assert result.is_valid, f"expected valid, got error={result.error!r}"


def test_v52d3_missing_id_rejected() -> None:
    m = _good_manifest()
    del m["id"]
    result = validate_manifest_dict(m)
    assert not result.is_valid
    assert "'id'" in result.error


def test_v52d3_empty_id_rejected() -> None:
    m = _good_manifest()
    m["id"] = ""
    result = validate_manifest_dict(m)
    assert not result.is_valid
    assert "non-empty" in result.error


def test_v52d3_missing_version_rejected() -> None:
    m = _good_manifest()
    del m["version"]
    result = validate_manifest_dict(m)
    assert not result.is_valid
    assert "'version'" in result.error


def test_v52d3_missing_install_rejected() -> None:
    m = _good_manifest()
    del m["install"]
    result = validate_manifest_dict(m)
    assert not result.is_valid
    assert "'install'" in result.error


def test_v52d3_missing_runtime_rejected() -> None:
    m = _good_manifest()
    del m["runtime"]
    result = validate_manifest_dict(m)
    assert not result.is_valid
    assert "'runtime'" in result.error


def test_v52d3_install_not_object_rejected() -> None:
    m = _good_manifest()
    m["install"] = "not an object"
    result = validate_manifest_dict(m)
    assert not result.is_valid
    assert "object" in result.error


def test_v52d3_top_level_not_object_rejected() -> None:
    result = validate_manifest_dict(["not", "an", "object"])  # type: ignore[arg-type]
    assert not result.is_valid
    assert "object" in result.error


# ─── Layer 2: runtime block ─────────────────────────────────────────────


def test_v52d3_runtime_command_podman_rejected() -> None:
    m = _bug_e_manifest()
    # Pin: standalone runtime check independently of scope.
    result = validate_runtime_block(m["runtime"])
    assert not result.is_valid
    assert "podman" in result.error
    assert "Bug E" in result.error


def test_v52d3_runtime_command_docker_rejected() -> None:
    rt = {"command": "docker", "args": ["run", "alpine"]}
    result = validate_runtime_block(rt)
    assert not result.is_valid
    assert "docker" in result.error


def test_v52d3_runtime_command_with_whitespace_rejected() -> None:
    rt = {"command": "  podman  ", "args": ["run"]}
    result = validate_runtime_block(rt)
    assert not result.is_valid


def test_v52d3_runtime_shell_without_dash_c_rejected() -> None:
    rt = {"command": "sh", "args": ["echo", "hello"]}
    result = validate_runtime_block(rt)
    assert not result.is_valid
    assert "-c" in result.error


def test_v52d3_runtime_shell_with_dash_c_allowed() -> None:
    rt = {"command": "sh", "args": ["-c", "python -m rl_server"]}
    result = validate_runtime_block(rt)
    assert result.is_valid


def test_v52d3_runtime_bash_without_dash_c_rejected() -> None:
    rt = {"command": "bash", "args": []}
    result = validate_runtime_block(rt)
    assert not result.is_valid


def test_v52d3_runtime_module_image_placeholder_in_args_rejected() -> None:
    rt = {
        "command": "python",
        "args": ["-m", "rl_server", "{module_image}"],
    }
    result = validate_runtime_block(rt)
    assert not result.is_valid
    assert "{module_image}" in result.error
    assert "Bug E" in result.error


def test_v52d3_runtime_module_image_placeholder_in_command_rejected() -> None:
    rt = {"command": "{module_image}", "args": []}
    result = validate_runtime_block(rt)
    assert not result.is_valid
    assert "{module_image}" in result.error


def test_v52d3_runtime_unknown_placeholder_in_args_rejected() -> None:
    rt = {
        "command": "python",
        "args": ["-m", "rl_server", "--port", "{not-a-known-thing}"],
    }
    result = validate_runtime_block(rt)
    assert not result.is_valid
    assert "{not-a-known-thing}" in result.error


def test_v52d3_runtime_known_placeholders_in_args_allowed() -> None:
    rt = {
        "command": "python",
        "args": [
            "-m",
            "rl_server",
            "--port",
            "{RL_SERVER_PORT}",
            "--project-root",
            "/data",
            "--log-path",
            "/data/logs/rl_events_{project_slug}.jsonl",
        ],
    }
    result = validate_runtime_block(rt)
    assert result.is_valid, f"expected valid, got {result.error!r}"


def test_v52d3_runtime_empty_command_allowed() -> None:
    rt = {"command": "", "args": []}
    result = validate_runtime_block(rt)
    assert result.is_valid


def test_v52d3_runtime_command_not_string_rejected() -> None:
    rt = {"command": 42, "args": []}
    result = validate_runtime_block(rt)
    assert not result.is_valid
    assert "string" in result.error


def test_v52d3_runtime_args_not_list_rejected() -> None:
    rt = {"command": "python", "args": "not a list"}
    result = validate_runtime_block(rt)
    assert not result.is_valid
    assert "list" in result.error


def test_v52d3_runtime_args_non_string_element_rejected() -> None:
    rt = {"command": "python", "args": ["-m", 42]}
    result = validate_runtime_block(rt)
    assert not result.is_valid
    assert "string" in result.error


# ─── Layer 3: install.scope coherence ───────────────────────────────────


def test_v52d3_scope_global_with_bare_template_ok() -> None:
    m = _good_manifest()
    m["install"]["scope"] = "global"
    m["runtime"]["container_name_template"] = "vct-rl-reranker"
    result = validate_install_scope_coherence(m)
    assert result.is_valid


def test_v52d3_scope_global_with_trailing_project_slug_ok() -> None:
    # The launcher's resolve_global_container_name strips a trailing
    # `-{project_slug}` for global modules — so the trailing form is
    # tolerated.
    m = _good_manifest()
    m["install"]["scope"] = "global"
    m["runtime"]["container_name_template"] = "vct-rl-reranker-{project_slug}"
    result = validate_install_scope_coherence(m)
    assert result.is_valid


def test_v52d3_scope_global_with_non_trailing_project_slug_rejected() -> None:
    m = _good_manifest()
    m["install"]["scope"] = "global"
    m["runtime"]["container_name_template"] = "{project_slug}-vct-rl-reranker"
    result = validate_install_scope_coherence(m)
    assert not result.is_valid
    assert "non-trailing" in result.error


def test_v52d3_scope_per_project_with_project_slug_ok() -> None:
    m = _good_manifest()
    m["install"]["scope"] = "per_project"
    m["runtime"]["container_name_template"] = "vct-rl-reranker-{project_slug}"
    result = validate_install_scope_coherence(m)
    assert result.is_valid


def test_v52d3_scope_invalid_value_rejected() -> None:
    m = _good_manifest()
    m["install"]["scope"] = "machine_wide"
    result = validate_install_scope_coherence(m)
    assert not result.is_valid
    assert "global" in result.error


def test_v52d3_scope_unset_with_bare_template_warns() -> None:
    m = _good_manifest()
    m["install"]["scope"] = None
    m["runtime"]["container_name_template"] = "vct-rl-reranker"
    result = validate_install_scope_coherence(m)
    assert result.is_valid  # warn, not fail
    assert len(result.warnings) == 1
    assert "project_slug" in result.warnings[0]


def test_v52d3_scope_unset_with_per_project_template_ok_no_warn() -> None:
    m = _good_manifest()
    m["install"]["scope"] = None
    m["runtime"]["container_name_template"] = "vct-rl-reranker-{project_slug}"
    result = validate_install_scope_coherence(m)
    assert result.is_valid
    assert result.warnings == []


# ─── End-to-end manifest validation ─────────────────────────────────────


def test_v52d3_bug_e_manifest_rejected_end_to_end() -> None:
    result = validate_manifest_dict(_bug_e_manifest())
    assert not result.is_valid
    # The runtime layer catches it first.
    assert "podman" in result.error.lower() or "{module_image}" in result.error


def test_v52d3_good_manifest_passes_end_to_end() -> None:
    result = validate_manifest_dict(_good_manifest())
    assert result.is_valid


# ─── File I/O ───────────────────────────────────────────────────────────


def test_v52d3_validate_manifest_file_missing(tmp_path: Path) -> None:
    result = validate_manifest_file(tmp_path / "does-not-exist.json")
    assert not result.is_valid
    assert "not found" in result.error


def test_v52d3_validate_manifest_file_invalid_json(tmp_path: Path) -> None:
    bad = tmp_path / "vct-module.json"
    bad.write_text("{ not valid json", encoding="utf-8")
    result = validate_manifest_file(bad)
    assert not result.is_valid
    assert "JSON" in result.error


def test_v52d3_validate_manifest_file_happy_path(tmp_path: Path) -> None:
    good = tmp_path / "vct-module.json"
    good.write_text(json.dumps(_good_manifest()), encoding="utf-8")
    result = validate_manifest_file(good)
    assert result.is_valid, f"unexpected error: {result.error!r}"


def test_v52d3_validate_manifest_file_bug_e_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "vct-module.json"
    bad.write_text(json.dumps(_bug_e_manifest()), encoding="utf-8")
    result = validate_manifest_file(bad)
    assert not result.is_valid


# ─── CLI subprocess interface (Rust caller) ─────────────────────────────


def test_v52d3_cli_valid_manifest_exits_0(tmp_path: Path) -> None:
    good = tmp_path / "vct-module.json"
    good.write_text(json.dumps(_good_manifest()), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "vco_lib.manifest_validation", str(good)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"expected exit 0, got {proc.returncode}; "
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    out = json.loads(proc.stdout)
    assert out["is_valid"] is True
    assert out["error"] is None


def test_v52d3_cli_invalid_manifest_exits_1(tmp_path: Path) -> None:
    bad = tmp_path / "vct-module.json"
    bad.write_text(json.dumps(_bug_e_manifest()), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "vco_lib.manifest_validation", str(bad)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1, (
        f"expected exit 1, got {proc.returncode}; "
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    out = json.loads(proc.stdout)
    assert out["is_valid"] is False
    assert out["error"] is not None
    # Operator-facing reason landed on stderr too.
    assert "REJECTED" in proc.stderr


def test_v52d3_cli_no_arg_exits_2() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "vco_lib.manifest_validation"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 2


def test_v52d3_cli_missing_file_exits_1(tmp_path: Path) -> None:
    # Caller passed a path that doesn't exist — invalid manifest
    # (exit 1), not invocation error (exit 2).
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "vco_lib.manifest_validation",
            str(tmp_path / "nope.json"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1
    out = json.loads(proc.stdout)
    assert out["is_valid"] is False
    assert "not found" in out["error"]


# ─── ValidationResult helpers ───────────────────────────────────────────


def test_v52d3_validation_result_ok_no_warnings() -> None:
    r = ValidationResult.ok()
    assert r.is_valid
    assert r.error is None
    assert r.warnings == []


def test_v52d3_validation_result_fail_with_reason() -> None:
    r = ValidationResult.fail("some reason")
    assert not r.is_valid
    assert r.error == "some reason"


def test_v52d3_validation_result_carries_warnings() -> None:
    r = ValidationResult.ok(warnings=["w1", "w2"])
    assert r.warnings == ["w1", "w2"]
