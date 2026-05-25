# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Byte-identical regression guard for ``vco_lib.env_template``.

Unlike ``tests/test_config_projection_byte_identical.py`` (which pins
Python output against the Rust ``write_project_env_files`` reference
implementation), this file pins the Phase 0.D writer against its OWN
golden output. There is no pre-existing Rust reference for the
block-replace marker contract — Phase 0.D is the SOURCE of truth for
``.env`` byte layout; the existing Rust ``ensure_project_env_template``
uses a DIFFERENT (append-only ``# added by vco YYYY-MM-DD``) format
that is being deprecated, so we can't pin parity against it.

What this test pins
~~~~~~~~~~~~~~~~~~~

  1. Marker constants don't drift. Changing
     :data:`ENV_TEMPLATE_BEGIN` / :data:`ENV_TEMPLATE_END` after ship
     breaks every existing user's ``.env`` (no in-place replace; managed
     blocks stack on every run). The constants are RESERVED — pinned
     here.
  2. Managed-block layout (every line, in order) for a canonical input.
     A diff against this golden tells reviewers exactly what changed.
  3. Idempotency under realistic input (a user's ``.env`` with arbitrary
     content above + below the markers): apply twice, assert bytes are
     identical.

When this test fails
~~~~~~~~~~~~~~~~~~~~

  * If the failure is in marker constants → REVERT. The constants must
    stay stable across releases.
  * If the failure is in golden layout → review carefully:
      - Did someone re-order keys? (Insertion order changed?)
      - Did someone add / remove a forensic comment?
      - Did someone change the trailing-newline convention?
    Update the golden iff the change is intentional and migration-safe
    (existing managed blocks should still be in-place-replaceable on
    the next call — i.e. BEGIN and END markers must still appear).
  * If the failure is in idempotency → that's a bug; the writer is no
    longer idempotent, which breaks the contract. Find and fix.

Run: pytest tests/test_env_template_byte_identical.py -v
"""

from __future__ import annotations

from pathlib import Path

from vco_lib.env_template import (
    ENV_TEMPLATE_BEGIN,
    ENV_TEMPLATE_END,
    apply_env_template,
)


# ─── Marker constants (frozen byte string — reserved across releases) ───


def test_marker_begin_is_frozen() -> None:
    """If this assertion ever fails, we've broken backwards-compat for
    every existing user's ``.env``. Revert the change."""
    assert ENV_TEMPLATE_BEGIN == (
        "# >>> VCO-MANAGED ENV (do not edit between markers) >>>"
    )


def test_marker_end_is_frozen() -> None:
    """Companion to ``test_marker_begin_is_frozen``."""
    assert ENV_TEMPLATE_END == "# <<< VCO-MANAGED ENV <<<"


# ─── Canonical input fixture ────────────────────────────────────────────


def _canonical_keys() -> dict[str, str]:
    """The pinned input fixture — matches the realistic shape
    :func:`project_env_template_from_db` produces for a project named
    "My Test" with default-everything (no peers, default ports)."""
    return {
        # Identity
        "PROJECT_NAME": "My Test",
        "CODE_GRAPH_PROJECT": "MyTest",
        # KG
        "KG_COLLECTION": "MyTest_KnowledgeGraph",
        "DEVELOPMENT_COLLECTION": "MyTest_Development",
        "SHARED_KG_COLLECTION": "VibeCodedOrchestrator_KnowledgeGraph",
        # Flags
        "SHARED_KG_WRITE_DISABLED": "false",
        "SHARED_KG_OPT_OUT": "false",
        # Embedding
        "ACTIVE_EMBEDDING": "qwen3",
        # Services
        "WEAVIATE_URL": "http://localhost:8081",
        "WEAVIATE_PORT": "8081",
        "OLLAMA_URL": "http://localhost:11435",
        "OLLAMA_PORT": "11435",
        "CODE_EMBED_URL": "http://localhost:11440",
        "CODE_EMBED_PORT": "11440",
    }


# ─── Golden output ──────────────────────────────────────────────────────
#
# The pinned byte-for-byte output of `apply_env_template(_canonical_keys(),
# project_folder=tmp_path)` against a fresh `tmp_path` with no prior `.env`.
#
# Update protocol when this changes intentionally:
#   1. Run the test, copy the diff from the failure output.
#   2. Re-derive the new golden by reading the test's actual bytes.
#   3. Paste into _GOLDEN below.
#   4. Re-run; should pass.
# Reviewer checklist for the PR:
#   - Marker constants unchanged.
#   - Key insertion order matches `_CANONICAL_ENV_TEMPLATE_KEYS`.
#   - Forensic comments still present above each KEY=VALUE.
#   - LF (not CRLF) line endings.

_GOLDEN = (
    "# >>> VCO-MANAGED ENV (do not edit between markers) >>>\n"
    "# added by vco — PROJECT_NAME=My Test\n"
    "PROJECT_NAME=My Test\n"
    "# added by vco — CODE_GRAPH_PROJECT=MyTest\n"
    "CODE_GRAPH_PROJECT=MyTest\n"
    "# added by vco — KG_COLLECTION=MyTest_KnowledgeGraph\n"
    "KG_COLLECTION=MyTest_KnowledgeGraph\n"
    "# added by vco — DEVELOPMENT_COLLECTION=MyTest_Development\n"
    "DEVELOPMENT_COLLECTION=MyTest_Development\n"
    "# added by vco — SHARED_KG_COLLECTION=VibeCodedOrchestrator_KnowledgeGraph\n"
    "SHARED_KG_COLLECTION=VibeCodedOrchestrator_KnowledgeGraph\n"
    "# added by vco — SHARED_KG_WRITE_DISABLED=false\n"
    "SHARED_KG_WRITE_DISABLED=false\n"
    "# added by vco — SHARED_KG_OPT_OUT=false\n"
    "SHARED_KG_OPT_OUT=false\n"
    "# added by vco — ACTIVE_EMBEDDING=qwen3\n"
    "ACTIVE_EMBEDDING=qwen3\n"
    "# added by vco — WEAVIATE_URL=http://localhost:8081\n"
    "WEAVIATE_URL=http://localhost:8081\n"
    "# added by vco — WEAVIATE_PORT=8081\n"
    "WEAVIATE_PORT=8081\n"
    "# added by vco — OLLAMA_URL=http://localhost:11435\n"
    "OLLAMA_URL=http://localhost:11435\n"
    "# added by vco — OLLAMA_PORT=11435\n"
    "OLLAMA_PORT=11435\n"
    "# added by vco — CODE_EMBED_URL=http://localhost:11440\n"
    "CODE_EMBED_URL=http://localhost:11440\n"
    "# added by vco — CODE_EMBED_PORT=11440\n"
    "CODE_EMBED_PORT=11440\n"
    "# <<< VCO-MANAGED ENV <<<\n"
)


def test_golden_managed_block_byte_identical(tmp_path: Path) -> None:
    """Fresh apply against canonical input produces the pinned bytes."""
    apply_env_template(_canonical_keys(), project_folder=tmp_path)
    actual = (tmp_path / ".env").read_text(encoding="utf-8")
    assert actual == _GOLDEN, (
        "Managed-block layout drift detected. If intentional, update "
        "_GOLDEN in this file. If accidental, fix the writer.\n"
        f"--- expected ---\n{_GOLDEN!r}\n"
        f"--- actual ---\n{actual!r}"
    )


def test_golden_idempotent_double_apply(tmp_path: Path) -> None:
    """Two applies against the same input produce the same bytes."""
    apply_env_template(_canonical_keys(), project_folder=tmp_path)
    first = (tmp_path / ".env").read_bytes()
    apply_env_template(_canonical_keys(), project_folder=tmp_path)
    second = (tmp_path / ".env").read_bytes()
    assert first == second


def test_golden_under_realistic_user_content(tmp_path: Path) -> None:
    """Pinning the realistic case: user content above + below the
    managed block survives an apply byte-for-byte. This is the
    end-to-end story:

      * User opened ``.env`` and edited it to add their own exports.
      * Launcher re-projected canonical env on a grant toggle.
      * User's edits MUST survive; managed block MUST be refreshed.
    """
    env_path = tmp_path / ".env"
    env_path.write_text(
        "# my custom .env header (user-added 2026-05-24)\n"
        "MY_PERSONAL_TOKEN=abc123\n"
        "\n"
        f"{ENV_TEMPLATE_BEGIN}\n"
        "# stale managed block from an earlier run\n"
        "KG_COLLECTION=Stale\n"
        f"{ENV_TEMPLATE_END}\n"
        "\n"
        "# more user-added content below\n"
        "EXTRA_OVERRIDE_FOR_THIS_PROJECT=true\n"
    )

    apply_env_template(_canonical_keys(), project_folder=tmp_path)
    after_first = env_path.read_text()

    # User content survived above + below the markers.
    assert "# my custom .env header (user-added 2026-05-24)" in after_first
    assert "MY_PERSONAL_TOKEN=abc123" in after_first
    assert "# more user-added content below" in after_first
    assert "EXTRA_OVERRIDE_FOR_THIS_PROJECT=true" in after_first
    # Managed block refreshed.
    assert "KG_COLLECTION=MyTest_KnowledgeGraph" in after_first
    assert "Stale" not in after_first
    # Exactly one BEGIN/END pair (no stacking).
    assert after_first.count(ENV_TEMPLATE_BEGIN) == 1
    assert after_first.count(ENV_TEMPLATE_END) == 1

    # Idempotent on second run.
    apply_env_template(_canonical_keys(), project_folder=tmp_path)
    after_second = env_path.read_text()
    assert after_first == after_second


def test_golden_lf_line_endings(tmp_path: Path) -> None:
    """Golden has LF only — confirms the documented Unix-style line
    endings constraint. Pinning here as well as in the unit tests so
    the regression test alone is enough to catch a Windows-build
    accidentally setting CRLF."""
    apply_env_template(_canonical_keys(), project_folder=tmp_path)
    raw_bytes = (tmp_path / ".env").read_bytes()
    assert b"\r\n" not in raw_bytes
    # And the golden itself has only LF (sanity check).
    assert b"\r\n" not in _GOLDEN.encode("utf-8")
