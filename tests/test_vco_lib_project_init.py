"""Tests for vco_lib.project_init — single source of truth for project
init helpers (PR 2 of project-init/update overhaul).

Covers:
  - sanitize_for_weaviate_class — edge cases (empty, hyphenated, leading
    digit, all-punctuation, non-ASCII).
  - derive_project_collection_names — exact dict shape.
  - Back-compat with install.py path-based helpers.
  - CLI entry point (`python -m vco_lib.project_init derive`).
  - Schema definitions — named-vector slots + indexNullState invariant.
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import install  # noqa: E402  — back-compat surface
from vco_lib import project_init  # noqa: E402


class SanitizeForWeaviateClassTests(unittest.TestCase):
    """sanitize_for_weaviate_class — single sanitizer, replaces both Python
    `_derive_project_kg_name` and Rust `sanitize_kg_collection`.
    """

    def test_simple_lowercase(self):
        self.assertEqual(project_init.sanitize_for_weaviate_class("foo"), "Foo")

    def test_space_separated(self):
        # "foo bar" → "FooBar" (each part PascalCased, concatenated).
        self.assertEqual(project_init.sanitize_for_weaviate_class("foo bar"), "FooBar")

    def test_underscore_separated(self):
        # FOO_BAR — splits on `_`, PascalCases each: "FOO" → "FOO" (rest
        # preserved), "BAR" → "BAR". Result: "FOOBAR" (existing behavior;
        # the regex strips on `_` runs but the case-preserve rule keeps
        # uppercase tails intact).
        self.assertEqual(project_init.sanitize_for_weaviate_class("FOO_BAR"), "FOOBAR")

    def test_hyphen_separated(self):
        self.assertEqual(
            project_init.sanitize_for_weaviate_class("my-project"),
            "MyProject",
        )

    def test_leading_digit_falls_back(self):
        # Weaviate class names must start with a letter. A leading-digit
        # name like "123abc" produces "123abc" after split (digits not
        # stripped), which fails the .isalpha() guard → fallback "vct".
        self.assertEqual(project_init.sanitize_for_weaviate_class("123abc"), "vct")

    def test_empty_falls_back(self):
        self.assertEqual(project_init.sanitize_for_weaviate_class(""), "vct")

    def test_all_punctuation_falls_back(self):
        self.assertEqual(project_init.sanitize_for_weaviate_class("---"), "vct")

    def test_non_ascii_letters_are_stripped(self):
        # Policy decision (documented in module docstring): the regex
        # `[^A-Za-z0-9]+` matches anything NOT in ASCII A-Z/a-z/0-9, so
        # non-ASCII letters like `é` are treated as separators (stripped).
        # "étude" → split → ["tude"] → "Tude". This matches existing
        # install.py behavior exactly — PR 2 did not introduce new
        # normalization. If we ever want unicode-aware sanitization, that
        # would be a separate behavior change with migration plan.
        self.assertEqual(project_init.sanitize_for_weaviate_class("étude"), "Tude")

    def test_already_pascalcase_preserved(self):
        self.assertEqual(
            project_init.sanitize_for_weaviate_class("VideoFrames"),
            "VideoFrames",
        )


class DeriveProjectCollectionNamesTests(unittest.TestCase):
    """derive_project_collection_names — canonical dict shape."""

    def test_videoframes_exact_dict(self):
        result = project_init.derive_project_collection_names("VideoFrames")
        self.assertEqual(
            result,
            {
                "kg_collection": "VideoFrames_KnowledgeGraph",
                "development_collection": "VideoFrames_Development",
                "project_name": "VideoFrames",
                "shared_kg_collection": "VibeCodedTools_KnowledgeGraph",
                "kg_basename": "VideoFrames",
            },
        )

    def test_project_name_is_raw_not_sanitized(self):
        # project_name field must preserve the user's input verbatim,
        # NOT the sanitized form. This is what 4 surfaces want for the
        # PROJECT_NAME env var.
        result = project_init.derive_project_collection_names("my-project")
        self.assertEqual(result["project_name"], "my-project")
        self.assertEqual(result["kg_basename"], "MyProject")
        self.assertEqual(result["kg_collection"], "MyProject_KnowledgeGraph")

    def test_dev_collection_uses_uppercase_d(self):
        # Drift bug B1 mitigation: "_Development" not "_development".
        result = project_init.derive_project_collection_names("anything")
        self.assertTrue(result["development_collection"].endswith("_Development"))

    def test_shared_kg_collection_constant(self):
        # The shared cross-project KG name is fixed.
        for name in ("foo", "MyTest", "VideoFrames"):
            self.assertEqual(
                project_init.derive_project_collection_names(name)[
                    "shared_kg_collection"
                ],
                "VibeCodedTools_KnowledgeGraph",
            )


class BackCompatWithInstallPyTests(unittest.TestCase):
    """The path-based helpers in install.py must still produce the same
    output after the refactor — existing tests rely on this.
    """

    def test_kg_name_back_compat_mytest(self):
        # install.py's `_derive_project_kg_name(Path("/x/y/MyTest"))`
        # historically returned "MyTest_KnowledgeGraph". The new module
        # must match.
        self.assertEqual(
            install._derive_project_kg_name(Path("/x/y/MyTest")),
            "MyTest_KnowledgeGraph",
        )
        # Same value via the public name-based API.
        self.assertEqual(
            project_init.derive_project_kg_name("MyTest"),
            "MyTest_KnowledgeGraph",
        )

    def test_kg_name_back_compat_hyphens(self):
        self.assertEqual(
            install._derive_project_kg_name(Path("/x/y/vibecoded-orchestrator")),
            "VibecodedOrchestrator_KnowledgeGraph",
        )

    def test_dev_name_back_compat(self):
        self.assertEqual(
            install._derive_project_dev_name(Path("/x/y/MyTest")),
            "MyTest_Development",
        )

    def test_fallback_path_back_compat(self):
        self.assertEqual(
            install._derive_project_kg_name(Path("/x/y/...")),
            "vct_KnowledgeGraph",
        )
        self.assertEqual(
            install._derive_project_kg_name(Path("/x/y/1foo")),
            "vct_KnowledgeGraph",
        )

    def test_safe_class_re_pattern_unchanged(self):
        # Tests in test_install_shared_containers.py reach into
        # install._SAFE_CLASS_RE — keep the pattern stable.
        self.assertEqual(install._SAFE_CLASS_RE.pattern, r"[^A-Za-z0-9]+")
        self.assertIs(install._SAFE_CLASS_RE, project_init._SAFE_CLASS_RE)


class CliEntryPointTests(unittest.TestCase):
    """`python -m vco_lib.project_init derive --name <n> --json` — Rust
    subprocess interface.
    """

    def test_derive_json_output_parseable(self):
        result = subprocess.run(
            [sys.executable, "-m", "vco_lib.project_init",
             "derive", "--name", "VideoFrames", "--json"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, msg=f"stderr={result.stderr}")
        # Stdout must be parseable JSON (Rust does serde_json::from_str).
        payload = json.loads(result.stdout)
        self.assertEqual(payload["kg_collection"], "VideoFrames_KnowledgeGraph")
        self.assertEqual(payload["development_collection"], "VideoFrames_Development")
        self.assertEqual(payload["project_name"], "VideoFrames")
        self.assertEqual(payload["shared_kg_collection"], "VibeCodedTools_KnowledgeGraph")
        self.assertEqual(payload["kg_basename"], "VideoFrames")

    def test_derive_human_readable_without_json(self):
        result = subprocess.run(
            [sys.executable, "-m", "vco_lib.project_init",
             "derive", "--name", "my-project"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            timeout=10,
        )
        self.assertEqual(result.returncode, 0)
        # Human form is `key=value` lines.
        self.assertIn("kg_collection=MyProject_KnowledgeGraph", result.stdout)
        self.assertIn("project_name=my-project", result.stdout)

    def test_derive_requires_name(self):
        result = subprocess.run(
            [sys.executable, "-m", "vco_lib.project_init", "derive", "--json"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            timeout=10,
        )
        # argparse exits with code 2 on missing required arg.
        self.assertEqual(result.returncode, 2)


class SchemaDefinitionTests(unittest.TestCase):
    """Schema invariants required by detect_kg_schema_drift."""

    def test_kg_class_has_three_named_vectors(self):
        schema = project_init.kg_class_definition("Foo")
        vec_config = schema["vectorConfig"]
        self.assertIn("qwen3_embed", vec_config)
        self.assertIn("ollama_embed", vec_config)
        self.assertIn("openai_embed", vec_config)
        for slot in ("qwen3_embed", "ollama_embed", "openai_embed"):
            self.assertEqual(vec_config[slot]["vectorIndexType"], "hnsw")

    def test_kg_class_has_index_null_state(self):
        # PR 2 surprise: existing install.py schema definitions did NOT
        # set indexNullState=True even though the drift detector
        # required it. Adding it here closes that loop on fresh installs.
        schema = project_init.kg_class_definition("Foo")
        self.assertEqual(
            schema["invertedIndexConfig"],
            {"indexNullState": True},
        )

    def test_development_class_has_three_named_vectors(self):
        schema = project_init.development_class_definition("FooDev")
        vec_config = schema["vectorConfig"]
        self.assertEqual(
            set(vec_config.keys()),
            {"qwen3_embed", "ollama_embed", "openai_embed"},
        )

    def test_development_class_has_index_null_state(self):
        schema = project_init.development_class_definition("FooDev")
        self.assertEqual(
            schema["invertedIndexConfig"],
            {"indexNullState": True},
        )

    def test_kg_class_required_properties_present(self):
        schema = project_init.kg_class_definition("Foo")
        prop_names = {p["name"] for p in schema["properties"]}
        # Spot-check the load-bearing properties (full set is fine to
        # add later; we mainly want regression detection on names).
        self.assertIn("title", prop_names)
        self.assertIn("content", prop_names)
        self.assertIn("file_path", prop_names)
        self.assertIn("typed_links", prop_names)
        self.assertIn("status", prop_names)

    def test_kg_class_name_threaded_through(self):
        # The `class` field carries the caller-supplied name verbatim.
        self.assertEqual(
            project_init.kg_class_definition("MyTest_KnowledgeGraph")["class"],
            "MyTest_KnowledgeGraph",
        )

    def test_install_py_shims_match_module(self):
        # The install.py shims must produce exactly the same dict.
        self.assertEqual(
            install._kg_class_definition("Foo"),
            project_init.kg_class_definition("Foo"),
        )
        self.assertEqual(
            install._development_class_definition("FooDev"),
            project_init.development_class_definition("FooDev"),
        )


if __name__ == "__main__":
    unittest.main()
