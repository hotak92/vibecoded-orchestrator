# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Byte-identical parity test: ``apply_project_env`` (Python) vs
``write_project_env_files`` (Rust).

The Python contract MUST produce byte-identical surface output to the
Rust legacy writer for the same input. This is the acceptance criterion
of Phase 0.B — until the parity holds, we can't flip production callers
to the Python CLI without risking on-disk diffs that confuse users
with "why did my settings.json reformat".

Strategy
~~~~~~~~

We can't easily invoke the Rust ``write_project_env_files`` from
Python (it lives inside the Tauri crate and requires cargo + the full
launcher build). Instead, this test verifies parity by REPRODUCING THE
SAME ASSERTIONS that the Rust unit tests make against their own
output, then asserting them against the Python writer's output.

The Rust tests pinned here are in
``launcher/src-tauri/src/commands/projects_v2.rs``:

  * ``write_project_env_files_creates_both_paths`` (L4476-4548) —
    pins the shape of ``.claude/env`` and ``.claude/settings.json``
    env block for ``ProjectEnvSettings::with_defaults("My Test")``.
  * ``build_claude_env_managed_block_emits_begin_end_markers`` (L6731+)
    — pins the BEGIN/END marker presence + format.
  * ``merge_claude_env_managed_block_no_prior_returns_managed_only``
    (L6748+) — pins the no-prior-file behaviour.

The Python writer must satisfy every one of these assertions when fed
the equivalent bundle. Any divergence is a contract violation.

When the parity test catches a divergence
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Fix the PYTHON side. Rust is the byte-layout source of truth until the
follow-up PR that flips production callers to the Python CLI. Once
production callers are migrated, the parity test can be reversed
(Python becomes the source of truth and Rust is the legacy adapter).

Run: pytest tests/test_config_projection_byte_identical.py -v
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vco_lib.config_projection import (
    CLAUDE_ENV_MANAGED_BEGIN,
    CLAUDE_ENV_MANAGED_END,
    apply_project_env,
)


# ─── Fixture: "My Test" project — the Rust test's pinned input ──────────


def _my_test_bundle(project_root: Path) -> dict:
    """Mirror of ``ProjectEnvSettings::with_defaults("My Test")``.

    Pulled from the Rust source:
      * project_name = "My Test"
      * KG_COLLECTION = "MyTest_KnowledgeGraph" (sanitized "My Test"
        → "MyTest" + "_KnowledgeGraph")
      * DEVELOPMENT_COLLECTION = "MyTest_Development"
      * SHARED_KG_COLLECTION = "VibeCodedOrchestrator_KnowledgeGraph"
        (the post-v0.2.23 B1 capital-C "VibeCoded" default)
      * SHARED_KG_WRITE_DISABLED / SHARED_KG_OPT_OUT = "false"
      * CODE_GRAPH_PROJECT = "MyTest"
      * ACTIVE_EMBEDDING = "qwen3"
      * WEAVIATE_URL = "http://localhost:8081"
      * OLLAMA_URL = "http://localhost:11435"
      * CODE_EMBED_URL = "http://localhost:11440"
    """
    return {
        "canonical_env": {
            "KG_COLLECTION": "MyTest_KnowledgeGraph",
            "DEVELOPMENT_COLLECTION": "MyTest_Development",
            "SHARED_KG_COLLECTION": "VibeCodedOrchestrator_KnowledgeGraph",
            "SHARED_KG_WRITE_DISABLED": "false",
            "SHARED_KG_OPT_OUT": "false",
            "PROJECT_NAME": "My Test",
            "CODE_GRAPH_PROJECT": "MyTest",
            "ACTIVE_EMBEDDING": "qwen3",
            "WEAVIATE_URL": "http://localhost:8081",
            "WEAVIATE_PORT": "8081",
            "OLLAMA_URL": "http://localhost:11435",
            "OLLAMA_PORT": "11435",
            "CODE_EMBED_URL": "http://localhost:11440",
            "CODE_EMBED_PORT": "11440",
        },
        "project_id": "my-test-fixture",
        "project_root": project_root,
    }


# ─── claude/env parity assertions (mirrors Rust L4500-4514) ─────────────


def test_parity_claude_env_contains_canonical_exports(tmp_path: Path) -> None:
    """Mirrors Rust assertions L4504-4514:
    ``claude/env`` must contain the canonical exports with the exact
    shell-export syntax ``export KEY="value"``."""
    bundle = _my_test_bundle(tmp_path)
    apply_project_env(bundle, surfaces=["claude_env"])

    env_raw = (tmp_path / ".claude" / "env").read_text()

    # Direct lifts from Rust test L4504-4509.
    assert 'export KG_COLLECTION="MyTest_KnowledgeGraph"' in env_raw
    assert 'export PROJECT_NAME="My Test"' in env_raw
    assert 'export CODE_GRAPH_PROJECT="MyTest"' in env_raw, env_raw
    assert 'export DEVELOPMENT_COLLECTION="MyTest_Development"' in env_raw
    # B5 (2026-04-30 cleanup): CONVERSATION_COLLECTION must NOT appear.
    assert "CONVERSATION_COLLECTION" not in env_raw
    # Shared-KG fields propagate.
    assert (
        'export SHARED_KG_COLLECTION="VibeCodedOrchestrator_KnowledgeGraph"'
        in env_raw
    )
    assert 'export SHARED_KG_WRITE_DISABLED="false"' in env_raw
    assert 'export SHARED_KG_OPT_OUT="false"' in env_raw


def test_parity_claude_env_has_managed_markers(tmp_path: Path) -> None:
    """Mirrors Rust ``build_claude_env_managed_block_emits_begin_end_markers``:
    the BEGIN and END markers must appear, with BEGIN on the first
    managed line and END after the exports."""
    bundle = _my_test_bundle(tmp_path)
    apply_project_env(bundle, surfaces=["claude_env"])

    env_raw = (tmp_path / ".claude" / "env").read_text()
    assert CLAUDE_ENV_MANAGED_BEGIN in env_raw
    assert CLAUDE_ENV_MANAGED_END in env_raw

    begin_idx = env_raw.find(CLAUDE_ENV_MANAGED_BEGIN)
    end_idx = env_raw.find(CLAUDE_ENV_MANAGED_END)
    assert begin_idx < end_idx, "BEGIN must precede END"

    # Every canonical export lives BETWEEN the markers, not outside.
    block = env_raw[begin_idx:end_idx]
    assert 'export KG_COLLECTION="MyTest_KnowledgeGraph"' in block
    assert 'export PROJECT_NAME="My Test"' in block


# ─── claude/settings.json env block parity (mirrors Rust L4520-4546) ────


def test_parity_claude_settings_env_block_has_canonical_values(
    tmp_path: Path,
) -> None:
    """Mirrors Rust assertions L4520-4546:
    ``.claude/settings.json`` must exist; ``parsed["env"]["KEY"]`` must
    match each canonical value verbatim."""
    bundle = _my_test_bundle(tmp_path)
    apply_project_env(bundle, surfaces=["claude_settings_json"])

    settings = tmp_path / ".claude" / "settings.json"
    assert settings.exists()

    parsed = json.loads(settings.read_text())
    env = parsed["env"]

    # 2026-05-01: KG_COLLECTION carries the FULL Weaviate class name.
    assert env["KG_COLLECTION"] == "MyTest_KnowledgeGraph"
    # PROJECT_NAME is the raw user-supplied name (NOT sanitized).
    assert env["PROJECT_NAME"] == "My Test"
    # PR-8 cross-PR handoff: CODE_GRAPH_PROJECT is the sanitized form.
    assert env["CODE_GRAPH_PROJECT"] == "MyTest"
    # Uppercase D for Development (case-sensitive Weaviate class).
    assert env["DEVELOPMENT_COLLECTION"] == "MyTest_Development"
    # B5 cleanup.
    assert "CONVERSATION_COLLECTION" not in env
    # Shared-KG fields.
    assert env["SHARED_KG_COLLECTION"] == "VibeCodedOrchestrator_KnowledgeGraph"
    assert env["SHARED_KG_WRITE_DISABLED"] == "false"
    assert env["SHARED_KG_OPT_OUT"] == "false"


def test_parity_claude_settings_no_vscode_sidewrite(tmp_path: Path) -> None:
    """Mirrors Rust assertion L4495-4498 (PR-27 default behaviour):

    Default ``apply_project_env`` (no explicit surfaces) must NOT
    create ``.vscode/settings.json`` as a side-effect. The third
    surface is opt-in only."""
    bundle = _my_test_bundle(tmp_path)
    apply_project_env(bundle)  # no surfaces arg = default

    assert not (tmp_path / ".vscode" / "settings.json").exists(), (
        "PR-27 parity: default apply_project_env must NOT create "
        ".vscode/settings.json (the historical claude-code.env surface "
        "was removed because it doesn't propagate to MCP subprocesses "
        "on Linux as of Claude Code 2.1.143)."
    )


# ─── Bug-4 regression parity (mirrors PR-3 / PR-145 deep-merge) ─────────


def test_parity_settings_json_preserves_user_env_keys(tmp_path: Path) -> None:
    """Mirrors PR-3 Commit 6 (2026-05-06) Bug-4 fix:

    The Rust writer deep-merges the env sub-block, preserving user-
    added keys. Python must too. Pre-PR-3 the whole env object was
    REPLACED, dropping user keys. The Rust regression test for this
    lives in the deep-merge helper tests; we recreate the scenario
    here at the apply_project_env boundary to assert parity at the
    SAME public surface a Rust caller would observe."""
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir()
    settings_path.write_text(json.dumps({
        "env": {
            "KG_COLLECTION": "OldStaleValue",
            "OPENAI_API_BASE": "https://my-custom-host",
            "MY_DEBUG_TOGGLE": "1",
        },
        "permissions": {"allow": ["Read(/)"]},
    }, indent=2))

    bundle = _my_test_bundle(tmp_path)
    apply_project_env(bundle, surfaces=["claude_settings_json"])

    parsed = json.loads(settings_path.read_text())
    # Canonical key OVERWRITTEN with launcher's resolved value.
    assert parsed["env"]["KG_COLLECTION"] == "MyTest_KnowledgeGraph"
    # User keys PRESERVED.
    assert parsed["env"]["OPENAI_API_BASE"] == "https://my-custom-host"
    assert parsed["env"]["MY_DEBUG_TOGGLE"] == "1"
    # Sibling block PRESERVED.
    assert parsed["permissions"] == {"allow": ["Read(/)"]}


def test_parity_claude_env_preserves_lines_outside_markers(tmp_path: Path) -> None:
    """Mirrors PR-3 Commit 6 ``merge_claude_env_managed_block``
    behaviour: user lines outside BEGIN/END are byte-preserved."""
    env_path = tmp_path / ".claude" / "env"
    env_path.parent.mkdir()
    env_path.write_text(
        "# user header note\n"
        'export USER_OVERRIDE="custom-value"\n'
        f"{CLAUDE_ENV_MANAGED_BEGIN}\n"
        "# stale managed content\n"
        'export KG_COLLECTION="OldStale"\n'
        f"{CLAUDE_ENV_MANAGED_END}\n"
        "# user trailer note\n"
    )

    bundle = _my_test_bundle(tmp_path)
    apply_project_env(bundle, surfaces=["claude_env"])

    new_text = env_path.read_text()
    # User content above markers preserved.
    assert new_text.startswith("# user header note\n")
    assert 'export USER_OVERRIDE="custom-value"' in new_text
    # Managed segment refreshed.
    assert 'export KG_COLLECTION="MyTest_KnowledgeGraph"' in new_text
    assert "OldStale" not in new_text
    # User trailer preserved.
    assert "# user trailer note" in new_text


# ─── JSON formatting parity (Rust serde_json::to_string_pretty) ─────────


def test_parity_settings_json_format_2_space_indent_no_trailing_newline(
    tmp_path: Path,
) -> None:
    """Mirrors Rust's ``serde_json::to_string_pretty(&v)`` byte layout:

      * 2-space indentation.
      * No trailing newline (``fs::write(path, pretty)`` writes the
        string verbatim — serde_json's pretty does NOT add a trailing
        newline).
      * Object keys are emitted in insertion order (matches Python's
        dict insertion-order + json.dumps).
    """
    bundle = _my_test_bundle(tmp_path)
    apply_project_env(bundle, surfaces=["claude_settings_json"])

    settings = tmp_path / ".claude" / "settings.json"
    raw = settings.read_text()

    # 2-space indent: every non-empty indented line starts with "  " * N.
    # We check that nested keys are indented 2 spaces deeper than parent.
    lines = raw.splitlines()
    # Find a line that's nested inside "env": should start with "    "
    # (2 spaces for "env" being inside root, plus 2 more for the env's
    # children).
    for line in lines:
        if '"KG_COLLECTION"' in line:
            # Should be indented 4 spaces (env -> KG_COLLECTION).
            assert line.startswith("    "), (
                f"expected 4-space indent for env child, got: {line!r}"
            )
            assert not line.startswith("     "), (
                f"expected exactly 4-space indent, got >4: {line!r}"
            )
            break
    else:
        pytest.fail("KG_COLLECTION not found in serialised settings.json")

    # No trailing newline.
    assert not raw.endswith("\n"), (
        "settings.json must not end with a newline (matches Rust's "
        "serde_json::to_string_pretty + fs::write byte layout)."
    )


# ─── Real-world fixture: realistic VCO_dev-like settings.json ───────────


def test_parity_realistic_settings_round_trip(tmp_path: Path) -> None:
    """Round-trip a realistic settings.json containing user-set keys
    (KG_BASE_DIR — observed in production VCO_dev) plus a hooks block.

    Asserts:
      * The realistic shape survives an apply.
      * A second apply with the same bundle is byte-identical
        (idempotency under realistic content).

    This is the integration-style proof: not just the synthetic 1-key
    test, but the actual shape we see in the wild."""
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir()
    # Realistic snapshot from VCO_dev (modulo path values).
    settings_path.write_text(json.dumps({
        "env": {
            "PROJECT_NAME": "VCODev",
            "CODE_GRAPH_PROJECT": "VCODev",
            "KG_COLLECTION": "VCODev_KnowledgeGraph",
            "SHARED_KG_COLLECTION": "VibeCodedTools_KnowledgeGraph",
            "DEVELOPMENT_COLLECTION": "VCODev_Development",
            "KG_BASE_DIR": "/home/user/Desktop/PROGETTI/VCO_dev",
        },
        "hooks": {
            "PreToolUse": [
                {"matcher": "Edit(*)", "hooks": [
                    {"type": "command", "command": ".claude/hooks/x.sh"}
                ]}
            ]
        },
        "permissions": {"allow": ["Read(/)", "Bash(*)"]},
    }, indent=2))

    bundle = _my_test_bundle(tmp_path)
    # Override defaults to match the existing keys so we can verify
    # canonical overwrite vs user-key preservation independently.
    bundle["canonical_env"]["KG_COLLECTION"] = "VCODev_KnowledgeGraph"

    apply_project_env(bundle, surfaces=["claude_settings_json"])
    first = settings_path.read_text()

    # Idempotency: second apply produces identical output.
    apply_project_env(bundle, surfaces=["claude_settings_json"])
    second = settings_path.read_text()
    assert first == second, "second apply must be byte-identical to first"

    parsed = json.loads(first)
    # User-added key survived.
    assert parsed["env"]["KG_BASE_DIR"] == "/home/user/Desktop/PROGETTI/VCO_dev"
    # Sibling top-level blocks survived.
    assert "hooks" in parsed
    assert parsed["permissions"] == {"allow": ["Read(/)", "Bash(*)"]}
    # Canonical key honoured (matches bundle value).
    assert parsed["env"]["KG_COLLECTION"] == "VCODev_KnowledgeGraph"
    # New canonical keys from the bundle were added.
    assert parsed["env"]["ACTIVE_EMBEDDING"] == "qwen3"
    assert parsed["env"]["WEAVIATE_URL"] == "http://localhost:8081"
