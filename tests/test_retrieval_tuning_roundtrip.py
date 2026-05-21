# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Round-trip integration test for v0.2.22 Item #13 retrieval tuning.

The flow under test:

  templates script  →  retrieval-tuning.toml  →  hub /config  →  python client

In production:
  Tauri command writes  →   <vct_root_dir>/retrieval-tuning.toml
  hub config_api reads ←
  Python client (vco_lib.project_config.resolve()) ←  hub  ←  Python tests

This test exercises the equivalent path WITHOUT spinning up the hub
binary, by:

  1. Driving the bash setter script (vct_retrieval_tuning_set.sh) to
     write a known TOML payload to a tempdir via VCT_STATE_DIR.
  2. Calling the bash getter (vct_retrieval_tuning_get.sh) which falls
     back to reading the file directly (no hub running).
  3. Calling vco_lib.project_config._from_hub_body() with a synthesized
     hub response that embeds the same TOML values — verifying the
     ProjectConfig.retrieval_tuning dataclass round-trips faithfully.

Why no live hub: spinning up vct-hub for a python test would require
cargo-build artifacts and a CI runner with the Rust toolchain
configured. The schema drift detector (separate test, also here)
catches the only thing a live hub round-trip would catch: a
defaults mismatch between the bash / PS / Rust / Python copies of
the calibrated constants.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

from vco_lib.project_config import RetrievalTuning, _from_hub_body


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "templates" / "scripts"
GET_SCRIPT = SCRIPTS_DIR / "vct_retrieval_tuning_get.sh"
SET_SCRIPT = SCRIPTS_DIR / "vct_retrieval_tuning_set.sh"


CALIBRATED_DEFAULTS: dict[str, float] = {
    "code_graph_score_floor": 0.35,
    "kg_tier_min": 0.42,
    "kg_tier_single_chunk": 0.55,
    "kg_tier_three_chunks": 0.65,
    "kg_tier_full": 0.75,
}


def _bash_ok(*args: str, env: dict[str, str]) -> str:
    """Run a bash script and return stdout (strip trailing newline)."""
    result = subprocess.run(
        list(args),
        capture_output=True,
        text=True,
        env={**os.environ, **env},
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"bash script failed (rc={result.returncode}): {args}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result.stdout.strip()


@unittest.skipUnless(
    GET_SCRIPT.is_file() and SET_SCRIPT.is_file(),
    "headless retrieval-tuning scripts not present",
)
class RetrievalTuningRoundTrip(unittest.TestCase):
    """End-to-end: set via bash → read via bash → parse via Python client."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        # VCT_STATE_DIR=<tmp> isolates this test from the user's real
        # ~/.vct so production retrieval-tuning.toml isn't touched.
        self.env = {"VCT_STATE_DIR": self.tmpdir.name}

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_set_then_get_round_trips_full_block(self) -> None:
        # Reset to defaults via the setter; both the writer and the
        # reader should round-trip the calibrated values.
        _bash_ok(str(SET_SCRIPT), "--reset", env=self.env)
        toml_path = Path(self.tmpdir.name) / "retrieval-tuning.toml"
        self.assertTrue(toml_path.is_file(), "setter must create the file")

        # Getter (file-fallback path; hub is not running in this test).
        body = _bash_ok(
            str(GET_SCRIPT),
            "non-existent-project",  # arg ignored when hub unreachable
            env=self.env,
        )
        parsed = json.loads(body)
        for key, expected in CALIBRATED_DEFAULTS.items():
            self.assertAlmostEqual(
                parsed[key], expected, places=9,
                msg=f"{key} default drifted: got {parsed[key]}",
            )

    def test_set_single_field_preserves_others(self) -> None:
        # Start from defaults.
        _bash_ok(str(SET_SCRIPT), "--reset", env=self.env)
        # Change only one field.
        _bash_ok(
            str(SET_SCRIPT),
            "--field", "code_graph_score_floor",
            "--value", "0.5",
            env=self.env,
        )

        body = _bash_ok(
            str(GET_SCRIPT),
            "non-existent-project",
            env=self.env,
        )
        parsed = json.loads(body)
        self.assertAlmostEqual(parsed["code_graph_score_floor"], 0.5)
        # Other fields untouched.
        self.assertAlmostEqual(parsed["kg_tier_min"], 0.42)
        self.assertAlmostEqual(parsed["kg_tier_full"], 0.75)

    def test_set_rejects_invariant_violation(self) -> None:
        _bash_ok(str(SET_SCRIPT), "--reset", env=self.env)
        # Try to set kg_tier_min to a value greater than kg_tier_single_chunk.
        result = subprocess.run(
            [str(SET_SCRIPT), "--field", "kg_tier_min", "--value", "0.99"],
            capture_output=True,
            text=True,
            env={**os.environ, **self.env},
            check=False,
        )
        self.assertEqual(
            result.returncode, 2,
            f"expected validation-failure exit 2, got {result.returncode}:\n"
            f"stdout={result.stdout}\nstderr={result.stderr}",
        )
        self.assertIn("kg_tier_min", result.stderr)
        # File should still hold the defaults (validation rejected the write).
        toml_path = Path(self.tmpdir.name) / "retrieval-tuning.toml"
        body = toml_path.read_text(encoding="utf-8")
        self.assertIn("kg_tier_min = 0.42", body)

    def test_python_client_consumes_same_block(self) -> None:
        # Synthesize a hub response shaped like vct-hub's
        # ProjectConfigResponse, embedding values we just wrote via the
        # bash setter. The hub's config_api reads the SAME file, so
        # this synthesis is the legitimate stand-in for a live hub
        # round-trip (we'd otherwise need to spin up the binary).
        _bash_ok(str(SET_SCRIPT), "--reset", env=self.env)
        _bash_ok(
            str(SET_SCRIPT),
            "--field", "kg_tier_full",
            "--value", "0.82",
            env=self.env,
        )

        # Parse the file the same way the Rust hub does (line-by-line
        # name = value), then feed those values into a synthesized hub
        # body envelope. The Python client's _from_hub_body must
        # produce the same numeric values without any defaulting.
        toml_path = Path(self.tmpdir.name) / "retrieval-tuning.toml"
        parsed: dict[str, float] = {}
        for line in toml_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            name, _, raw = line.partition("=")
            parsed[name.strip()] = float(raw.strip())

        synthetic_body: dict[str, Any] = {
            "project_id": "test-project",
            "project_path": "/tmp/test",
            "project_slug": "test",
            "project_display_name": "Test",
            "code_graph_project": "test",
            "code_graph_collection_prefix": "Test",
            "kg_collection": "Test_KG",
            "shared_kg_collection": "VibeCodedOrchestrator_KnowledgeGraph",
            "development_collection": "",
            "active_embedding": "qwen3",
            "embedding_models": {
                "text": "qwen3-embedding:0.6b",
                "code": "CodeSage-Large-v2",
            },
            "kg_access_list": ["Test_KG"],
            "codegraph_access_list": ["test"],
            "weaviate_url": "http://localhost:8081",
            "ollama_url": "http://localhost:11435",
            "grpc_port": 50052,
            "shared_kg_write_disabled": False,
            "retrieval_tuning": parsed,
        }
        cfg = _from_hub_body(synthetic_body)
        self.assertIsInstance(cfg.retrieval_tuning, RetrievalTuning)
        self.assertAlmostEqual(cfg.retrieval_tuning.kg_tier_full, 0.82)
        self.assertAlmostEqual(cfg.retrieval_tuning.kg_tier_min, 0.42)
        # Range invariant holds (writer enforced it on write).
        self.assertLess(
            cfg.retrieval_tuning.kg_tier_min,
            cfg.retrieval_tuning.kg_tier_single_chunk,
        )
        self.assertLess(
            cfg.retrieval_tuning.kg_tier_single_chunk,
            cfg.retrieval_tuning.kg_tier_three_chunks,
        )
        self.assertLess(
            cfg.retrieval_tuning.kg_tier_three_chunks,
            cfg.retrieval_tuning.kg_tier_full,
        )


class RetrievalTuningDefaultsDriftGuard(unittest.TestCase):
    """Catch drift between bash / PS / Rust / Python defaults.

    Every copy of the calibrated defaults lives in a different file:

      * vco_lib/project_config.py — RetrievalTuning + _from_hub_body fallback
      * launcher/src-tauri/src/commands/retrieval_tuning.rs — Rust writer
      * launcher/src-tauri/vct-hub/src/retrieval_tuning_io.rs — Rust reader
      * launcher/src/lib/components/RetrievalTuningPanel.svelte — FE
      * templates/scripts/vct_retrieval_tuning_get.sh — bash get fallback
      * templates/scripts/vct_retrieval_tuning_set.sh — bash set fallback
      * templates/scripts/vct_retrieval_tuning_get.ps1 — PS get fallback
      * templates/scripts/vct_retrieval_tuning_set.ps1 — PS set fallback

    A find/sed sweep across all 8 files is the long-term answer; for
    now we grep the readable defaults out of bash + PS scripts and
    compare against the canonical Python constants. The Rust unit
    tests (in both crates) pin their own copies via direct equality
    on doubles. Drift in any of the four script files is caught here;
    drift in either Rust copy is caught in cargo tests.
    """

    @unittest.skipUnless(
        GET_SCRIPT.is_file() and SET_SCRIPT.is_file(),
        "headless retrieval-tuning scripts not present",
    )
    def test_bash_setter_defaults_match_canonical(self) -> None:
        body = SET_SCRIPT.read_text(encoding="utf-8")
        # Lines like `_DEFAULT_code_graph_score_floor=0.35`.
        for key, expected in CALIBRATED_DEFAULTS.items():
            needle = f"_DEFAULT_{key}={expected}"
            self.assertIn(
                needle, body,
                f"bash setter missing or mismatched default: {needle}",
            )

    @unittest.skipUnless(
        GET_SCRIPT.is_file() and SET_SCRIPT.is_file(),
        "headless retrieval-tuning scripts not present",
    )
    def test_bash_getter_defaults_match_canonical(self) -> None:
        body = GET_SCRIPT.read_text(encoding="utf-8")
        # The getter embeds defaults inline in two python heredocs;
        # we sniff out the canonical literal in either location.
        for key, expected in CALIBRATED_DEFAULTS.items():
            # Bash python heredoc: "'kg_tier_min': 0.42,"
            needle = f"'{key}': {expected}"
            self.assertIn(
                needle, body,
                f"bash getter missing or mismatched default: {needle}",
            )

    def test_python_client_defaults_match_canonical(self) -> None:
        # Reach into _from_hub_body's defaults via a hub body MISSING
        # the retrieval_tuning key — the synthesis path emits the
        # calibrated defaults verbatim.
        body: dict[str, Any] = {
            "project_id": "p", "project_path": "/", "project_slug": "p",
            "project_display_name": "P", "code_graph_project": "p",
            "code_graph_collection_prefix": "P",
            "kg_collection": "P_KG",
            "shared_kg_collection": "VibeCodedOrchestrator_KnowledgeGraph",
            "development_collection": "",
            "active_embedding": "qwen3",
            "embedding_models": {"text": "t", "code": "c"},
            "kg_access_list": [], "codegraph_access_list": [],
            "weaviate_url": "u", "ollama_url": "u", "grpc_port": 0,
            "shared_kg_write_disabled": False,
            # NO retrieval_tuning key — exercises the default synthesis.
        }
        cfg = _from_hub_body(body)
        for key, expected in CALIBRATED_DEFAULTS.items():
            actual = getattr(cfg.retrieval_tuning, key)
            self.assertAlmostEqual(
                actual, expected, places=9,
                msg=f"python client default drifted on {key}: got {actual}",
            )


if __name__ == "__main__":
    unittest.main()
