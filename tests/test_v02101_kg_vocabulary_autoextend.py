# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.101 lane 2A (P299-A3 residual) — vocabulary auto-extend + loud skip summary.

The 2026-09 field incident: a project's sync held **377 of 546** disk nodes
because every node whose frontmatter ``type:`` (``source``, ``opinion``,
``technical``, …) was not declared in ``knowledge/VOCABULARY.md`` got
rejected — whole folders silently missing from search. The DROP itself was
already fixed (validation warns but never blocks; the MCP store path
re-routes instead of rejecting). What was still missing — and what this
lane delivers — is the owner-directed behaviour (P299:95-103):

1. **Auto-extend** — an unknown-but-wellformed ``type:`` is INGESTED and the
   type is APPENDED to the project's ``knowledge/VOCABULARY.md`` (user data:
   append-only, never rewrite existing bytes), with a visible report line.
   ONE home for the decision: ``vco_lib.kg_vocabulary``
   (``classify_node_type`` / ``extend_vocabulary``), called from BOTH KG
   write paths (``sync_knowledge_graph.sync_node`` and the weaviate-kg MCP's
   ``store_knowledge_node``) and mirrored by the drift scanner's skip
   predicate so behaviour is identical across paths.
2. **Still-invalid types stay loud** — an empty/malformed ``type:`` is never
   stored and never silently skipped: it FAILS (sync) or is REFUSED (store),
   and lands in the end-of-run ``📋 N of M items not synced this run …``
   summary (counts per reason category, every path named with its reason;
   "items" is the honest noun — M includes excluded non-nodes such as meta
   files, which the breakdown then names). A refused node leaves NO file
   side effect: the invalid-type refusal runs BEFORE the ``updated:``
   timestamp write (GLM wave-2 nit 3).

Red-proof
---------
Pre-fix (HEAD 6ff4d774) the ACT-side tests fail: an undeclared-type node
syncs but the vocabulary is NEVER extended (no ``extend_vocabulary`` exists),
an empty-type node is STORED with ``node_type: ""`` instead of failing, the
store path accepts any garbage type, the drift scanner has no invalid-type
bucket, and the summary line reads ``N not-synced item(s)`` without the
``of M`` total. Mutations that must turn these red again:

* remove the ``extend_vocabulary`` call from ``sync_node`` → the
  auto-declare assertions fail;
* remove the invalid-type gate → the loud-failure assertions fail;
* collapse the ``📋`` line back to the no-total shape → the summary
  assertion fails;
* remove the drift skip predicate → the phantom-drift assertion fails.

Fixture discipline (source-text-gates lesson): the vocabulary fixture is the
REAL shipped ``templates/knowledge/VOCABULARY.md`` text, never a parser
assumption. No test contacts a live Weaviate (fake clients / injected query
functions throughout, per tests/conftest.py's sentinel).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(REPO_ROOT), str(REPO_ROOT / "claude_mcp_servers")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from vco_lib import kg_vocabulary as kv  # noqa: E402

# Harness reuse (search-before-add): the sync-run fake-Weaviate harness from
# the v0.2.92 tally tests and the store_knowledge_node fakes from the W8
# wiring test. Module imports only — their TestCase classes are NOT pulled
# into this module's namespace, so pytest does not re-collect them here.
import tests.test_v0292_kg_sync_flags_tally_stage as tally_h  # noqa: E402
import tests.test_wiring_w8_shared_chunk_plan as w8h  # noqa: E402

from vco_lib import kg_sync_drift as drift  # noqa: E402

TEMPLATE_PATH = REPO_ROOT / "templates" / "knowledge" / "VOCABULARY.md"
REAL_TEMPLATE = TEMPLATE_PATH.read_text(encoding="utf-8")

#: The type from the original incident: wellformed, undeclared.
UNDECLARED_TYPE = "source-person"
EXPECTED_HEADING = "#### **`co:SourcePerson`** (alias: `source-person`)"


def _md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def _write_vocab(root: Path) -> Path:
    v = root / "knowledge" / "VOCABULARY.md"
    v.parent.mkdir(parents=True, exist_ok=True)
    v.write_text(REAL_TEMPLATE, encoding="utf-8")
    return v


def _node_text(title: str, type_line: str | None) -> str:
    fm = [f"title: {title}"]
    if type_line is not None:
        fm.append(type_line)
    fm.append("status: active")
    return "---\n" + "\n".join(fm) + "\n---\nBody text.\n"


# ═══════════════════════════════════════════════════════════════════════════
# A. vco_lib.kg_vocabulary — the ONE home (classify + extend)
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def _fresh_vocab_cache():
    kv.clear_vocabulary_cache()
    yield
    kv.clear_vocabulary_cache()


class TestClassifyNodeType:
    def test_known_types_case_insensitive(self):
        vocab = kv.builtin_vocabulary()
        assert kv.classify_node_type("concept", vocab) == kv.TYPE_KNOWN
        # The declaration parser lowercases every alias — `Concept` and
        # `concept` are the SAME type, so the gate must not call it unknown.
        assert kv.classify_node_type("Concept", vocab) == kv.TYPE_KNOWN
        assert kv.classify_node_type(" concept ", vocab) == kv.TYPE_KNOWN

    def test_wellformed_undeclared_is_extendable(self):
        vocab = kv.builtin_vocabulary()
        for t in (UNDECLARED_TYPE, "opinion", "technical", "Source_Person2"):
            assert kv.classify_node_type(t, vocab) == kv.TYPE_EXTENDABLE

    def test_empty_or_malformed_is_invalid(self):
        vocab = kv.builtin_vocabulary()
        for bad in ("", "   ", None, 42, ["x"], {"a": 1}, "has space",
                    "slash/ed", "dot.ted", "emoji🚀"):
            assert kv.classify_node_type(bad, vocab) == kv.TYPE_INVALID, bad

    def test_declared_custom_alias_is_known(self):
        vocab = kv.parse_vocabulary_text(REAL_TEMPLATE + "\n" + EXPECTED_HEADING + "\n")
        assert kv.classify_node_type(UNDECLARED_TYPE, vocab) == kv.TYPE_KNOWN


class TestExtendVocabulary:
    def test_extend_appends_parseable_declaration(self, tmp_path):
        root = tmp_path / "proj"
        v = _write_vocab(root)
        before = v.read_bytes()

        res = kv.extend_vocabulary(root, [UNDECLARED_TYPE])

        assert res.error == ""
        assert res.added == (UNDECLARED_TYPE,)
        after = v.read_bytes()
        # APPEND-ONLY: every original byte preserved as the exact prefix.
        assert after[: len(before)] == before
        assert EXPECTED_HEADING in after.decode("utf-8")
        # Round-trip through the REAL parser: the appended heading declares.
        kv.clear_vocabulary_cache()
        assert UNDECLARED_TYPE in kv.load_vocabulary(root).node_types

    def test_extend_creates_missing_vocabulary_file(self, tmp_path):
        root = tmp_path / "proj"
        (root / "knowledge").mkdir(parents=True)
        res = kv.extend_vocabulary(root, ["opinion"])
        assert res.added == ("opinion",)
        v = root / "knowledge" / "VOCABULARY.md"
        assert v.exists()
        kv.clear_vocabulary_cache()
        assert "opinion" in kv.load_vocabulary(root).node_types

    def test_already_declared_type_is_noop_byte_identical(self, tmp_path):
        root = tmp_path / "proj"
        v = _write_vocab(root)
        before = _md5(v)
        res = kv.extend_vocabulary(root, ["concept", "Concept"])
        assert res.added == ()
        assert set(res.already_declared) == {"concept"}
        assert _md5(v) == before  # an unchanged run never touches the file

    def test_invalid_types_recorded_never_appended(self, tmp_path):
        root = tmp_path / "proj"
        v = _write_vocab(root)
        before = v.read_bytes()
        res = kv.extend_vocabulary(root, ["", "has space", None])
        assert res.added == ()
        assert len(res.invalid) == 3
        assert v.read_bytes() == before

    def test_idempotent_second_call_adds_nothing(self, tmp_path):
        root = tmp_path / "proj"
        _write_vocab(root)
        kv.extend_vocabulary(root, [UNDECLARED_TYPE])
        v = root / "knowledge" / "VOCABULARY.md"
        after_first = v.read_bytes()
        res2 = kv.extend_vocabulary(root, [UNDECLARED_TYPE])
        assert res2.added == ()
        assert res2.already_declared == (UNDECLARED_TYPE,)
        assert v.read_bytes() == after_first  # no duplicate heading

    def test_batch_dedupes_and_mixed_classifies(self, tmp_path):
        root = tmp_path / "proj"
        _write_vocab(root)
        res = kv.extend_vocabulary(
            root, ["opinion", "Opinion", "technical", "concept", ""]
        )
        assert res.added == ("opinion", "technical")
        assert res.already_declared == ("concept",)
        assert len(res.invalid) == 1

    def test_class_name_collision_falls_back_without_duplicating(
        self, tmp_path
    ):
        """GLM wave-2 nit 5: when BOTH the Pascal form and the raw alias are
        already used as `co:` class names (under other aliases), the new
        declaration takes a free numeric suffix instead of duplicating a
        heading name."""
        root = tmp_path / "proj"
        v = root / "knowledge" / "VOCABULARY.md"
        v.parent.mkdir(parents=True)
        v.write_text(
            REAL_TEMPLATE
            + "\n#### **`co:SourcePerson`** (alias: `sp-pascal`)\n"
            + "\n#### **`co:source-person`** (alias: `sp-raw`)\n",
            encoding="utf-8",
        )
        res = kv.extend_vocabulary(root, [UNDECLARED_TYPE])
        assert res.added == (UNDECLARED_TYPE,)
        text = v.read_text(encoding="utf-8")
        assert "#### **`co:source-person-2`** (alias: `source-person`)" in text
        # No co: name declared twice by a real heading line.
        seen = set()
        for line in text.splitlines():
            m = kv._CLASS_HEADING_RE.match(line)
            if not m:
                continue
            name = m.group("name")
            assert name not in seen, f"duplicate co: heading name {name!r}"
            seen.add(name)
        # The alias still round-trips through the real parser.
        kv.clear_vocabulary_cache()
        assert UNDECLARED_TYPE in kv.load_vocabulary(root).node_types

    def test_write_failure_soft_fails_with_error(self, tmp_path):
        root = tmp_path / "proj"
        # knowledge/VOCABULARY.md is a DIRECTORY → the append write raises
        # OSError → returned as .error, never propagated.
        (root / "knowledge" / "VOCABULARY.md").mkdir(parents=True)
        res = kv.extend_vocabulary(root, ["opinion"])
        assert res.added == ()
        assert res.error != ""

    def test_no_trailing_newline_file_still_appends_cleanly(self, tmp_path):
        root = tmp_path / "proj"
        v = root / "knowledge" / "VOCABULARY.md"
        v.parent.mkdir(parents=True)
        v.write_text("# Vocab\nno trailing newline", encoding="utf-8")
        before = v.read_bytes()
        res = kv.extend_vocabulary(root, ["opinion"])
        assert res.added == ("opinion",)
        after = v.read_bytes()
        assert after[: len(before)] == before
        text = after.decode("utf-8")
        # The appended block starts on its own line, and the heading parses.
        assert "\nno trailing newline\n" in text
        kv.clear_vocabulary_cache()
        assert "opinion" in kv.load_vocabulary(root).node_types


# ═══════════════════════════════════════════════════════════════════════════
# B. sync path — templates/scripts/sync_knowledge_graph.py::sync_node
# ═══════════════════════════════════════════════════════════════════════════

class SyncVocabularyGateTests(tally_h._SyncTestBase):
    """Runs the REAL sync main() against the in-memory fake backends."""

    def _write_node(self, rel: str, title: str, type_line: str | None) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(_node_text(title, type_line), encoding="utf-8")
        return p

    def _kg_rows(self, harness):
        return tally_h._rows(harness.last, tally_h.PROJECT_KG)

    def test_undeclared_type_syncs_and_vocabulary_is_extended(self):
        """ACT (incident shape): an undeclared-but-wellformed `type:` is
        INGESTED and the type is DECLARED afterwards (P299:95-99)."""
        vocab = _write_vocab(self.root)
        before = vocab.read_bytes()
        self._write_node(
            "knowledge/concepts/person.md", "Person Source",
            f"type: {UNDECLARED_TYPE}",
        )
        mod = self.load()
        harness = self.install_working_backends(mod)
        code, out, _err = self.run_main(mod, ["kg-sync", "--all"])

        self.assertEqual(code, 0, f"undeclared type must not fail the run:\n{out}")
        # The node REACHED Weaviate (pre-fix it did too — the drop is gone)…
        rows = self._kg_rows(harness)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].properties.get("node_type"), UNDECLARED_TYPE)
        # …and the vocabulary was AUTO-EXTENDED (pre-fix: never).
        after = vocab.read_bytes()
        self.assertEqual(after[: len(before)], before, "append-only violated")
        self.assertIn(EXPECTED_HEADING, after.decode("utf-8"))
        kv.clear_vocabulary_cache()
        self.assertIn(UNDECLARED_TYPE, kv.load_vocabulary(self.root).node_types)
        # Visible report line (never a silent vocabulary edit).
        self.assertIn("Auto-declared node type", out)
        self.assertIn(UNDECLARED_TYPE, out)

    def test_invalid_type_fails_loudly_in_not_synced_summary(self):
        """ACT: an empty/malformed frontmatter `type:` is INVALID — failed
        loudly, named in the `N of M items not synced` summary, never
        stored, never silently skipped (P299:100-103)."""
        _write_vocab(self.root)
        self._write_node("knowledge/concepts/good.md", "Good", "type: concept")
        self._write_node("knowledge/concepts/empty.md", "Empty", 'type: ""')
        mod = self.load()
        harness = self.install_working_backends(mod)
        code, out, _err = self.run_main(mod, ["kg-sync", "--all"])

        self.assertEqual(code, 1, "an invalid type must fail the run")
        # The invalid node never reached Weaviate; the good one did.
        rows = self._kg_rows(harness)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].properties.get("title"), "Good")
        # The end-of-run summary names it with reason + the N-of-M shape.
        # M (3) counts everything the run CONSIDERED: good + empty + the
        # fixture's own VOCABULARY.md (excluded meta file, by design).
        self.assertIn(
            "📋 2 of 3 items not synced this run (--all): "
            "1 failed, 1 excluded-skipped", out
        )
        self.assertIn("knowledge/concepts/empty.md", out)
        self.assertIn("invalid node type", out)

    def test_malformed_type_charset_fails_loudly(self):
        _write_vocab(self.root)
        self._write_node("knowledge/concepts/sp.md", "Spaced", 'type: "has space"')
        mod = self.load()
        harness = self.install_working_backends(mod)
        code, out, _err = self.run_main(mod, ["kg-sync", "--all"])
        self.assertEqual(code, 1)
        self.assertEqual(self._kg_rows(harness), [])
        # 2 of 2: the failed node + the fixture's excluded VOCABULARY.md.
        self.assertIn("2 of 2 items not synced", out)
        self.assertIn("invalid node type", out)

    def test_invalid_type_refusal_leaves_no_file_side_effect(self):
        """GLM wave-2 nit 3: the refusal runs BEFORE the `updated:` timestamp
        write — a node that can never sync must not be touched by the run
        that refuses it. The spy proves the ORDERING (pre-fix the timestamp
        step ran for the invalid node); the md5 proves the user-visible
        guarantee (the file comes back byte-identical)."""
        _write_vocab(self.root)
        bad = self._write_node(
            "knowledge/concepts/empty.md", "Empty", 'type: ""'
        )
        before = _md5(bad)
        mod = self.load()
        harness = self.install_working_backends(mod)
        ts_calls: list = []
        real_ts = mod._update_frontmatter_timestamp

        def _spy(fp, c):
            ts_calls.append(str(fp))
            return real_ts(fp, c)

        mod._update_frontmatter_timestamp = _spy
        code, out, _err = self.run_main(mod, ["kg-sync", "--all"])
        self.assertEqual(code, 1)
        self.assertEqual(self._kg_rows(harness), [])
        self.assertIn("invalid node type", out)
        # The refused node's file: never handed to the timestamp step…
        self.assertNotIn(str(bad), ts_calls)
        # …and byte-identical on disk.
        self.assertEqual(_md5(bad), before)

    def test_frontmatter_archived_wins_over_invalid_type(self):
        """Precedence preserved: an archived node is deliberately not
        indexed, so its (invalid) type never fails the run — the historical
        frontmatter-archive skip handles it."""
        _write_vocab(self.root)
        p = self.root / "knowledge" / "concepts" / "arch.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            '---\ntitle: Arch\ntype: ""\nstatus: archived\n---\nBody.\n',
            encoding="utf-8",
        )
        mod = self.load()
        harness = self.install_working_backends(mod)
        code, out, _err = self.run_main(mod, ["kg-sync", "--all"])
        self.assertEqual(code, 0, f"archived must not fail the run:\n{out}")
        self.assertEqual(self._kg_rows(harness), [])
        self.assertIn("Skipping (frontmatter)", out)
        self.assertNotIn("invalid node type", out)

    def test_known_types_run_never_touches_vocabulary_file(self):
        """LEAVE-ALONE (byte-preservation): a run whose types are all
        declared leaves VOCABULARY.md byte-identical (hash before/after)."""
        vocab = _write_vocab(self.root)
        before = _md5(vocab)
        self._write_node("knowledge/concepts/a.md", "A", "type: concept")
        self._write_node("knowledge/tools/b.md", "B", "type: tool")
        mod = self.load()
        harness = self.install_working_backends(mod)
        code, _out, _err = self.run_main(mod, ["kg-sync", "--all"])
        self.assertEqual(code, 0)
        self.assertEqual(len(self._kg_rows(harness)), 2)
        self.assertEqual(_md5(vocab), before)

    def test_folder_derived_type_keeps_legacy_warn_only(self):
        """LEAVE-ALONE: with NO frontmatter `type:` key the parser derives
        the type from the folder — that value is NEVER auto-declared (it
        would pollute the vocabulary with folder names nobody chose) and
        NEVER failed; the historical warn-only path stays."""
        vocab = _write_vocab(self.root)
        before = _md5(vocab)
        self._write_node("knowledge/concepts/plain.md", "Plain", None)
        mod = self.load()
        harness = self.install_working_backends(mod)
        code, out, _err = self.run_main(mod, ["kg-sync", "--all"])
        self.assertEqual(code, 0)
        rows = self._kg_rows(harness)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].properties.get("node_type"), "concepts")
        self.assertEqual(_md5(vocab), before)
        self.assertIn("not declared", out)  # legacy warning still teaches

    def test_version_skew_keeps_legacy_warn_only_no_crash(self):
        """VERSION SKEW: with vco_lib.kg_vocabulary unimportable the gate
        degrades to the historical warn-only behaviour — never extend,
        never fail, never crash."""
        vocab = _write_vocab(self.root)
        before = _md5(vocab)
        self._write_node(
            "knowledge/concepts/person.md", "Person", f"type: {UNDECLARED_TYPE}"
        )
        mod = self.load()
        harness = self.install_working_backends(mod)
        saved = sys.modules.get("vco_lib.kg_vocabulary", "absent")
        sys.modules["vco_lib.kg_vocabulary"] = None  # forces ImportError
        try:
            code, out, _err = self.run_main(mod, ["kg-sync", "--all"])
        finally:
            if saved == "absent":
                sys.modules.pop("vco_lib.kg_vocabulary", None)
            else:
                sys.modules["vco_lib.kg_vocabulary"] = saved
            kv.clear_vocabulary_cache()
        self.assertEqual(code, 0, "skew must not fail nodes")
        self.assertEqual(len(self._kg_rows(harness)), 1)
        self.assertEqual(_md5(vocab), before)
        self.assertIn("not declared", out)


# ═══════════════════════════════════════════════════════════════════════════
# C. store path — claude_mcp_servers/weaviate_mcp/server.py
# ═══════════════════════════════════════════════════════════════════════════

def _store_node(monkeypatch, tmp_path, *, node_type, title="Store Test",
                content="Body text.", file_path="", scope="project"):
    """Run store_knowledge_node against an in-memory collection (the W8
    harness shape, with node_type as a parameter). Returns
    ``(result_dict, store_dict, srv)``."""
    srv = w8h._mcp_server()
    store: dict = {}
    coll = w8h._Collection(store)

    class _FakeClient:
        collections = types.SimpleNamespace(get=lambda _n: coll)

    monkeypatch.setattr(srv, "get_weaviate_client", lambda: _FakeClient())
    monkeypatch.setattr(srv, "Filter", w8h._Filt)
    monkeypatch.setattr(srv, "KG_BASE_DIR", str(tmp_path))
    monkeypatch.setattr(srv, "EMBEDDING_SOURCE", "ollama")
    monkeypatch.setattr(srv, "DUAL_EMBEDDING_ENABLED", False)
    monkeypatch.setattr(srv, "_emit_gate_skipped_metric", lambda *a, **k: None)
    monkeypatch.setattr(srv, "_emit_gate_skipped_deferral", lambda *a, **k: None)
    monkeypatch.setattr(srv, "_cached_embed_service", w8h._SlotStub())
    monkeypatch.delenv("VCT_PROJECT_ID", raising=False)
    monkeypatch.setenv("ACTIVE_EMBEDDING", "qwen3")
    monkeypatch.setenv("EMBEDDING_MODEL", w8h.QWEN3_MODEL)

    async def _tagged(text):  # noqa: ARG001
        return {"qwen3_embed": [0.5, 0.5]}, []

    async def _plain(text):  # noqa: ARG001
        return [0.5, 0.5]

    monkeypatch.setattr(srv, "_get_all_kg_embeddings_tagged", _tagged)
    monkeypatch.setattr(srv, "get_embedding", _plain)
    kv.clear_vocabulary_cache()

    fn = w8h._unwrap(srv.store_knowledge_node)
    result = json.loads(asyncio.run(fn(
        title=title, content=content, node_type=node_type, tags=["v02101"],
        links=[], file_path=file_path, scope=scope,
    )))
    kv.clear_vocabulary_cache()
    return result, store, srv


@pytest.fixture()
def store_root(tmp_path):
    _write_vocab(tmp_path)
    return tmp_path


def test_store_undeclared_type_succeeds_and_extends_vocabulary(
    monkeypatch, store_root
):
    """ACT: the MCP store path applies the SAME shared decision — ingest +
    auto-declare, with the extension visible in the tool result."""
    vocab = store_root / "knowledge" / "VOCABULARY.md"
    before = vocab.read_bytes()
    result, store, _srv = _store_node(
        monkeypatch, store_root, node_type=UNDECLARED_TYPE,
        file_path=f"knowledge/concepts/{UNDECLARED_TYPE}-node.md",
    )
    assert result.get("success") is True, result
    assert result.get("vocabulary_extended") == [UNDECLARED_TYPE]
    assert len(store) == 1
    row = next(iter(store.values()))
    assert row.properties.get("node_type") == UNDECLARED_TYPE
    after = vocab.read_bytes()
    assert after[: len(before)] == before  # append-only
    assert EXPECTED_HEADING.encode("utf-8") in after


def test_store_invalid_type_refused_before_any_write(monkeypatch, store_root):
    """ACT: an empty/malformed type is REFUSED loudly — no Weaviate row, no
    .md file, file_written honestly False."""
    vocab = store_root / "knowledge" / "VOCABULARY.md"
    before = _md5(vocab)
    for bad in ("", "has space"):
        store: dict = {}
        result, store, _srv = _store_node(
            monkeypatch, store_root, node_type=bad,
            file_path="knowledge/concepts/bad.md",
        )
        assert result.get("success") is not True, (bad, result)
        assert result.get("file_written") is False
        assert "invalid node_type" in result.get("error", "")
        assert store == {}, f"type {bad!r} must never be stored"
        assert not (store_root / "knowledge" / "concepts" / "bad.md").exists()
    assert _md5(vocab) == before


def test_store_known_type_leaves_vocabulary_byte_identical(
    monkeypatch, store_root
):
    """LEAVE-ALONE: a declared type never touches the vocabulary file."""
    vocab = store_root / "knowledge" / "VOCABULARY.md"
    before = _md5(vocab)
    result, store, _srv = _store_node(
        monkeypatch, store_root, node_type="concept",
        file_path="knowledge/concepts/known.md",
    )
    assert result.get("success") is True
    assert "vocabulary_extended" not in result
    assert len(store) == 1
    assert _md5(vocab) == before


def test_store_version_skew_accepts_anything_no_extension(
    monkeypatch, store_root
):
    """VERSION SKEW: kg_vocabulary unimportable → the gate stays OFF and
    the historical accept-everything behaviour is preserved."""
    vocab = store_root / "knowledge" / "VOCABULARY.md"
    before = _md5(vocab)
    monkeypatch.setitem(sys.modules, "vco_lib.kg_vocabulary", None)
    try:
        result, store, _srv = _store_node(
            monkeypatch, store_root, node_type="any garbage type",
            file_path="knowledge/concepts/skew.md",
        )
    finally:
        kv.clear_vocabulary_cache()
    assert result.get("success") is True
    assert len(store) == 1
    assert _md5(vocab) == before


# ═══════════════════════════════════════════════════════════════════════════
# D. drift scanner — vco_lib/kg_sync_drift.py (phantom-drift reconciliation)
# ═══════════════════════════════════════════════════════════════════════════

def _drift_scan(tmp_path, nodes: dict, stored: dict | None = None):
    root = tmp_path / "proj"
    for rel, content in nodes.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return drift.scan_drift(
        root / "knowledge",
        weaviate_url="http://127.0.0.1:9",
        # No `_<Family>` suffix on purpose: the drift scan never touches
        # Weaviate (query fn is injected), and a bare literal would have to
        # be tabled in the fixture-class guard's inventory.
        kg_collection="V02101DriftKG",
        reachable_fn=lambda _u: True,
        query_hashes_fn=lambda _u, _c, on_warn=None: dict(stored or {}),
    )


def test_drift_skips_invalid_type_nodes_no_phantom_missing(tmp_path):
    """A node sync FAILS for an invalid type can never reach Weaviate —
    drift must count it in its own bucket, never report it as missing."""
    report = _drift_scan(tmp_path, {
        "knowledge/concepts/empty.md": _node_text("Empty", 'type: ""'),
        "knowledge/concepts/good.md": _node_text("Good", "type: concept"),
    })
    assert report.invalid_type_skipped == 1
    assert "knowledge/concepts/empty.md" not in report.drifted
    assert "knowledge/concepts/good.md" in report.missing  # genuinely owed


def test_drift_extendable_type_stays_checkable(tmp_path):
    """An undeclared-but-wellformed type SYNCS (auto-extend), so drift must
    keep checking it — skipping it would hide a real drop."""
    report = _drift_scan(tmp_path, {
        "knowledge/concepts/p.md": _node_text("P", f"type: {UNDECLARED_TYPE}"),
    })
    assert report.invalid_type_skipped == 0
    assert "knowledge/concepts/p.md" in report.missing


def test_drift_absent_type_key_stays_checkable(tmp_path):
    """No frontmatter `type:` → sync folder-derives and never fails →
    drift must not skip."""
    report = _drift_scan(tmp_path, {
        "knowledge/concepts/plain.md": _node_text("Plain", None),
    })
    assert report.invalid_type_skipped == 0
    assert report.missing == ("knowledge/concepts/plain.md",)


def test_drift_invalid_predicate_parity_with_shared_home(tmp_path):
    """PARITY PIN: the drift skip predicate is the SHARED decision —
    for every value shape, drift skips exactly what the one-home gate
    classifies INVALID (and nothing else)."""
    cases = [
        ('type: ""', True),
        ("type:", True),               # YAML null
        ('type: "has space"', True),
        ("type: [a, b]", True),
        ("type: 42", True),
        (f"type: {UNDECLARED_TYPE}", False),
        ("type: concept", False),
        (None, False),                 # no type key at all
    ]
    vocab = kv.builtin_vocabulary()
    for type_line, expect_skip in cases:
        content = _node_text("N", type_line)
        present, raw = drift._frontmatter_type_declared(content)
        if not present:
            assert expect_skip is False, type_line
            continue
        assert drift.node_type_invalid(content) is expect_skip, type_line
        # The SHARED home made the same call:
        assert (
            kv.classify_node_type(raw, vocab) == kv.TYPE_INVALID
        ) is expect_skip, type_line


def test_drift_metadata_dialect_invalid_type_also_skipped(tmp_path):
    """The nested `metadata:` dialect is promoted by sync's parser — the
    drift predicate must see the promoted key too."""
    content = (
        "---\nname: nested\nmetadata:\n  type: \"\"\n---\nBody.\n"
    )
    report = _drift_scan(tmp_path, {"knowledge/concepts/n.md": content})
    assert report.invalid_type_skipped == 1
    assert report.drifted == ()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
