# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The WRITER↔READER contract for the chat-model context table (v0.2.92, WP-11).

Two processes, two languages, one file. The launcher (Rust) WRITES
``<vct_root>/model-gateway/chat_model_context.json``; the model gateway
(Python) READS it. Each side resolves the path and parses the document with
its own code, so nothing in either language can prove on its own that they
agree — which is exactly the class of defect that ships silently: the writer
keeps refreshing a file the reader never looks at, and the gateway keeps
serving its bundled fallback while the GUI shows a table the user edited.

This is the parity test the repo's A>B>C rule requires for a (C)-tier mirror.
The mirror was chosen deliberately over (A) shared code: making the launcher
shell out to Python to resolve a two-segment path would make its BOOT seed
depend on the gateway package being importable — backwards, since the launcher
writes a file the gateway may never be installed to read.

What is pinned here:

1. The three path literals (state subdirectory, basename, env override) are
   character-identical in both sources.
2. The schema version the writer stamps is one the reader accepts.
3. The ``source`` marker the writer stamps is the one the reader's documented
   contract names.
4. Every per-model field the writer emits is a field the reader reads, and
   every field the reader requires is one the writer emits.
5. **The strongest one**: a document assembled from the RUST source's own key
   names, carrying the real shipped seed's rows, is fed to the REAL
   ``ContextTableLoader`` — which must accept it as an EXPORT (not fall back
   to its bundled seed) and resolve the 1M flags correctly.

Sibling coverage, deliberately not duplicated here:
``tests/test_model_router_context_table.py`` owns the seed's own properties
(ten cited rows, the 1M set, the exact-lookup rule); the Rust unit tests own
the store, the migration and the export builder.
"""
from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from model_router import config as gw_config
from model_router import context_table as ct

_REPO = Path(__file__).resolve().parent.parent

_RUST_CMD = (
    _REPO / "launcher" / "src-tauri" / "src" / "commands" / "chat_model_context.rs"
)
_RUST_CORE = (
    _REPO
    / "launcher"
    / "src-tauri"
    / "vct-launcher-core"
    / "src"
    / "db"
    / "chat_model_context.rs"
)
_PY_CONFIG = _REPO / "claude_mcp_servers" / "model_router" / "config.py"
_SEED = _REPO / "claude_mcp_servers" / "model_router" / "chat_model_context.seed.json"


def _rust_str_const(source: Path, name: str) -> str:
    """Read a `const NAME: &str = "value";` literal out of a Rust file."""
    text = source.read_text(encoding="utf-8")
    m = re.search(rf'const {name}: &str = "([^"]*)";', text)
    assert m, f"{name} not found as a &str const in {source}"
    return m.group(1)


def _rust_u64_const(source: Path, name: str) -> int:
    text = source.read_text(encoding="utf-8")
    m = re.search(rf"const {name}: u64 = (\d+);", text)
    assert m, f"{name} not found as a u64 const in {source}"
    return int(m.group(1))


def _rust_export_entry_keys() -> list[str]:
    """The per-model keys `export_document` inserts, in insertion order.

    Read out of the source rather than hardcoded here so that ADDING a field
    on the Rust side without teaching the reader about it fails this test
    instead of shipping a field nothing consumes.
    """
    text = _RUST_CORE.read_text(encoding="utf-8")
    body = text.split("pub fn export_document(", 1)[1]
    # Stop before the top-level document keys (`let mut doc =`).
    body = body.split("let mut doc", 1)[0]
    return re.findall(r'entry\.insert\(\s*"([a-z_0-9]+)"', body)


def _rust_export_doc_keys() -> list[str]:
    text = _RUST_CORE.read_text(encoding="utf-8")
    body = text.split("let mut doc = serde_json::Map::new();", 1)[1]
    body = body.split("serde_json::Value::Object(doc)", 1)[0]
    return re.findall(r'doc\.insert\(\s*"([a-z_0-9]+)"', body)


class PathLiteralParityTests(unittest.TestCase):
    """The writer and the reader must resolve the SAME file."""

    def test_state_subdirectory_matches(self) -> None:
        self.assertEqual(
            _rust_str_const(_RUST_CMD, "GATEWAY_STATE_SUBDIR"),
            gw_config._STATE_SUBDIR,
        )

    def test_export_basename_matches(self) -> None:
        self.assertEqual(
            _rust_str_const(_RUST_CMD, "EXPORT_BASENAME"),
            gw_config._EXPORT_BASENAME,
        )

    def test_env_override_name_matches(self) -> None:
        """A knob honoured by only one end silently splits the pair: the
        launcher would refresh a file nothing reads while the gateway waited
        on a file nothing writes."""
        rust_env = _rust_str_const(_RUST_CMD, "EXPORT_PATH_ENV")
        py_source = _PY_CONFIG.read_text(encoding="utf-8")
        self.assertIn(
            f'os.environ.get("{rust_env}")',
            py_source,
            "the Rust override env var is not the one config.py::export_path reads",
        )

    def test_the_composed_default_path_is_the_same_on_both_sides(self) -> None:
        """Compose the reader's default from its own helpers and check the
        Rust literals reproduce its last two segments."""
        reader_default = gw_config.export_path()
        self.assertEqual(
            reader_default.name, _rust_str_const(_RUST_CMD, "EXPORT_BASENAME")
        )
        self.assertEqual(
            reader_default.parent.name,
            _rust_str_const(_RUST_CMD, "GATEWAY_STATE_SUBDIR"),
        )

    def test_the_seed_lives_where_the_rust_loader_looks_for_it(self) -> None:
        text = _RUST_CMD.read_text(encoding="utf-8")
        m = re.search(
            r"const SEED_RELATIVE_PATH: \[&str; \d+\] = \[(.*?)\];", text, re.S
        )
        assert m, "SEED_RELATIVE_PATH not found"
        segments = re.findall(r'"([^"]+)"', m.group(1))
        self.assertEqual(
            (_REPO.joinpath(*segments)).resolve(),
            _SEED.resolve(),
            "the launcher would look for the shipped seed somewhere it is not",
        )
        # Segments are composed with `join`, never a literal "a/b/c" — the
        # Windows half of the tri-OS bar.
        for seg in segments:
            self.assertNotIn("/", seg)
            self.assertNotIn("\\", seg)


class SchemaParityTests(unittest.TestCase):
    def test_the_writers_schema_version_is_one_the_reader_accepts(self) -> None:
        written = _rust_u64_const(_RUST_CORE, "EXPORT_SCHEMA_VERSION")
        self.assertIn(
            written,
            ct.SUPPORTED_SCHEMA_VERSIONS,
            "the launcher would stamp a version the gateway refuses, and every "
            "install would silently fall back to the bundled seed",
        )

    def test_the_source_marker_matches_the_readers_contract(self) -> None:
        written = _rust_str_const(_RUST_CORE, "EXPORT_SOURCE_LAUNCHER_DB")
        self.assertEqual(written, "launcher.db")
        self.assertIn(
            '"source": "launcher.db"',
            ct.__doc__ or "",
            "the reader's documented file contract names a different marker",
        )

    def test_every_field_the_writer_emits_is_a_field_the_reader_reads(self) -> None:
        reader_src = Path(ct.__file__).read_text(encoding="utf-8")
        parse_body = reader_src.split("def _parse(", 1)[1].split("\nclass ", 1)[0]
        for key in _rust_export_entry_keys():
            self.assertIn(
                f'"{key}"',
                parse_body,
                f"the writer emits `{key}` but the reader never looks at it — "
                f"either wire it or stop writing it",
            )

    def test_every_field_the_reader_requires_is_one_the_writer_emits(self) -> None:
        emitted = set(_rust_export_entry_keys())
        # `source_note` is optional on the wire (omitted when empty), so it is
        # emitted CONDITIONALLY — assert it is present in the source at all.
        for required in ("vendor", "context_window", "max_output", "window_1m", "source"):
            self.assertIn(required, emitted, f"the writer never emits `{required}`")
        self.assertIn("source_note", emitted)

    def test_the_top_level_document_keys_are_the_contracts(self) -> None:
        self.assertEqual(
            _rust_export_doc_keys(),
            ["schema_version", "generated_at", "source", "models"],
            "top-level shape (and, under serde_json's preserve_order, the "
            "on-disk key ORDER) drifted from the documented contract",
        )


class RoundTripThroughTheRealReaderTests(unittest.TestCase):
    """Assemble the document the way the Rust writer does — using the KEY
    NAMES read out of the Rust source — and hand it to the real loader."""

    def _document_from_seed(self) -> dict:
        seed = json.loads(_SEED.read_text(encoding="utf-8"))
        entry_keys = _rust_export_entry_keys()
        models: dict[str, dict] = {}
        for model_id in sorted(k for k in seed["models"] if not k.startswith("_")):
            src = seed["models"][model_id]
            entry: dict[str, object] = {}
            for key in entry_keys:
                if key == "source_note":
                    # The writer omits an empty note.
                    if src.get("source_note"):
                        entry[key] = src["source_note"]
                    continue
                entry[key] = src[key]
            models[model_id] = entry
        doc: dict[str, object] = {}
        for key in _rust_export_doc_keys():
            doc[key] = {
                "schema_version": _rust_u64_const(_RUST_CORE, "EXPORT_SCHEMA_VERSION"),
                "generated_at": "2026-09-02T18:04:11Z",
                "source": _rust_str_const(_RUST_CORE, "EXPORT_SOURCE_LAUNCHER_DB"),
                "models": models,
            }[key]
        return doc

    def test_the_reader_accepts_it_as_an_export_not_a_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "chat_model_context.json"
            path.write_text(json.dumps(self._document_from_seed()), encoding="utf-8")

            table = ct.ContextTableLoader(path).current()

            self.assertEqual(
                table.source,
                ct.SOURCE_EXPORT,
                "the reader fell back to its bundled seed — the export the "
                "launcher writes is not one it will use",
            )
            self.assertEqual(table.path, path)
            self.assertEqual(table.uncited, ())
            # Ten cited vendor rows + the four first-party Claude 5 rows
            # (pinned in tests/test_model_router_context_table.py).
            self.assertEqual(len(table.rows), 14)

    def test_the_one_m_decisions_survive_the_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "chat_model_context.json"
            path.write_text(json.dumps(self._document_from_seed()), encoding="utf-8")
            table = ct.ContextTableLoader(path).current()

            # The version-key evidence, end to end through the writer's shape.
            self.assertTrue(table.advertise_1m("glm-5.2"))
            self.assertFalse(table.advertise_1m("glm-5.1"))
            # And still EXACT: no family stem or longer-id match. (`glm-5`
            # is itself a real row, so the stem probed here is `glm`.)
            self.assertIsNone(table.lookup("glm"))
            self.assertIsNone(table.lookup("glm-5.2-flash"))

    def test_the_citation_caveats_survive_the_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "chat_model_context.json"
            path.write_text(json.dumps(self._document_from_seed()), encoding="utf-8")
            table = ct.ContextTableLoader(path).current()

            air = table.lookup("glm-4.5-air")
            assert air is not None
            self.assertIn("404", air.source_note)
            self.assertIn("NOT official", air.source_note)
            self.assertFalse(
                air.window_1m,
                "third-party listings of a 1M glm-4.5-air are not official and "
                "must not survive a round trip as one",
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
