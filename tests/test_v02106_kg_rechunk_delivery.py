# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.106 — the chunker-revision re-chunk reaches every install ONCE, and
never drops a node before its new vectors exist (owner ruling 2026-11-11,
"fix + re-embed").

The chain these tests pin, piece by piece:

1. ``vco_lib.chunker_revision.kg_rechunk_owed`` — the ONE question both
   update paths ask: the ledger carries ``chunker_preset_overhaul_pending``
   (the bundle gate emitted it on the revision crossing) AND the KG half has
   not been stamped under the current revision. A second update answers
   False, an unreadable state answers None (never "owed").
2. ``kg-sync --check-drift`` reports it on its sentinel (``rechunk_owed``),
   which the launcher's bundle update turns into the one hash-gated
   ``--all`` pass (Rust: ``DriftVerdict::RechunkOwed``); ``install.py``'s
   root seed asks the same helper (``install_weaviate.kg_rechunk_due_now``).
3. ``sync_node`` / ``sync_doc`` embed EVERY new chunk before deleting the old
   rows: a failed embed leaves the node intact.
4. The code-graph half: a completed whole-repo ``--force-rewalk`` (what the
   0.2.106 extractor-generation rung triggers on update) stamps it, and the
   printed remedy is that non-destructive walk, not ``--force-recreate``.

RED-PROOF (copies of the pre-change files): (1) does not exist before
(AttributeError); (2) the pre-change sentinel carries no ``rechunk_owed``
field; (3) pre-change ``sync_node`` deletes first — the failing-embed test
finds zero rows; (4) ``record_successful_walk`` ignores a re-walk and the
remedy prints ``--force-recreate``.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from tests.test_v0292_kg_chunk_plan_transition import (
    BODY_SINGLE_TO_MULTI,
    DEV_COLL,
    PROJECT_KG,
    _CountingServer,
    _load_sync_module,
    _PlanTransitionTestBase,
    _rows_for,
    _seed_pending_ledger,
    _seed_rows,
    _write_doc,
    _write_node,
)


class _FailingServer(_CountingServer):
    """Embeds the first ``ok`` texts, then the backend 'goes down'."""

    def __init__(self, ok: int):
        super().__init__()
        self._ok = ok

    def _get_all_kg_embeddings_tagged(self, text):  # noqa: ARG002
        if self.embed_calls >= self._ok:
            raise RuntimeError("embedding backend unreachable (test)")
        return super()._get_all_kg_embeddings_tagged(text)

    def _get_all_kg_embeddings(self, text):  # noqa: ARG002
        if self.embed_calls >= self._ok:
            raise RuntimeError("embedding backend unreachable (test)")
        return super()._get_all_kg_embeddings(text)


class RechunkOwedTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.folder = Path(self._tmp.name)

    def test_no_ledger_entry_means_nothing_is_owed(self):
        from vco_lib.chunker_revision import kg_rechunk_owed

        self.assertIs(kg_rechunk_owed(self.folder), False)

    def test_entry_without_a_current_kg_stamp_is_owed_once(self):
        from vco_lib import chunker_revision as cr

        _seed_pending_ledger(self.folder)
        self.assertIs(cr.kg_rechunk_owed(self.folder), True)
        # A stamp from the PREVIOUS revision does not count.
        cr.record_resync_half(self.folder, "kg", revision="v0.2.92.1")
        self.assertIs(cr.kg_rechunk_owed(self.folder), True)
        # The KG half run under the current revision: never owed again, even
        # while the entry waits for its code-graph half.
        cr.record_resync_half(self.folder, "kg")
        self.assertIs(cr.kg_rechunk_owed(self.folder), False)

    def test_unreadable_stamp_is_unknown_never_owed(self):
        from vco_lib import chunker_revision as cr

        _seed_pending_ledger(self.folder)
        cr.resync_state_path(self.folder).parent.mkdir(parents=True, exist_ok=True)
        cr.resync_state_path(self.folder).write_text("{not json", encoding="utf-8")
        self.assertIsNone(cr.kg_rechunk_owed(self.folder))

    def test_install_root_leg_asks_the_same_question(self):
        from vco_lib import chunker_revision as cr
        from vco_lib import install_weaviate as iw

        self.assertFalse(iw.kg_rechunk_due_now(self.folder))
        _seed_pending_ledger(self.folder)
        self.assertTrue(iw.kg_rechunk_due_now(self.folder))
        cr.record_resync_half(self.folder, "kg")
        self.assertFalse(iw.kg_rechunk_due_now(self.folder))


class DriftSentinelTests(_PlanTransitionTestBase):
    def _sentinel(self, mod, **kwargs) -> dict:
        class _B:
            status = "bound"
            kg_collection = PROJECT_KG
            detail = ""

        class _R:
            status = "ok"
            scanned = 3
            missing = stale = stored_only = ()
            detail = ""

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            mod._print_drift_sentinel(_B(), _R(), **kwargs)
        line = [ln for ln in buf.getvalue().splitlines()
                if ln.startswith(mod.DRIFT_SENTINEL_PREFIX)][-1]
        return json.loads(line[len(mod.DRIFT_SENTINEL_PREFIX):])

    def test_the_sentinel_carries_rechunk_owed_only_when_positively_true(self):
        mod = _load_sync_module(self.root)
        self.assertIs(self._sentinel(mod, rechunk_owed=True)["rechunk_owed"], True)
        self.assertIs(self._sentinel(mod, rechunk_owed=None)["rechunk_owed"], False)
        self.assertIs(self._sentinel(mod)["rechunk_owed"], False)


class EmbedBeforeDeleteTests(_PlanTransitionTestBase):
    def test_a_failed_embed_leaves_the_node_rows_in_place(self):
        """Multi-chunk re-chunk whose SECOND embed fails: the old row must
        survive (pre-fix the delete ran first and the node vanished)."""
        node = self.root / "knowledge" / "concepts" / "mid.md"
        text = _write_node(node, "Mid Node", BODY_SINGLE_TO_MULTI)
        _seed_pending_ledger(self.root)
        mod = _load_sync_module(self.root)
        server = _FailingServer(ok=1)
        fp = "knowledge/concepts/mid.md"
        content_hash = mod._content_signature_excluding_updated(text)
        _seed_rows(server, PROJECT_KG, fp, content_hash, [text], total_chunks=1)

        outcome, out = self._sync_node(mod, server, node)
        self.assertEqual(outcome.status, mod.OUTCOME_FAILED, out)
        rows = _rows_for(server, PROJECT_KG, fp)
        self.assertEqual([r["content"] for r in rows], [text],
                         "the old row must survive a failed re-embed")

    def test_a_failed_single_embed_leaves_the_row_in_place(self):
        node = self.root / "knowledge" / "concepts" / "small.md"
        text = _write_node(node, "Small Node", "One short body. Two sentences.")
        mod = _load_sync_module(self.root)
        server = _FailingServer(ok=0)
        fp = "knowledge/concepts/small.md"
        # Stale hash → the node is re-embedded; the embed fails.
        _seed_rows(server, PROJECT_KG, fp, "stale-hash", ["old text"], total_chunks=1)

        outcome, out = self._sync_node(mod, server, node)
        self.assertEqual(outcome.status, mod.OUTCOME_FAILED, out)
        self.assertEqual([r["content"] for r in _rows_for(server, PROJECT_KG, fp)],
                         ["old text"])
        self.assertTrue(text)

    def test_a_successful_rechunk_replaces_the_rows(self):
        node = self.root / "knowledge" / "concepts" / "mid.md"
        text = _write_node(node, "Mid Node", BODY_SINGLE_TO_MULTI)
        _seed_pending_ledger(self.root)
        mod = _load_sync_module(self.root)
        server = _CountingServer()
        fp = "knowledge/concepts/mid.md"
        content_hash = mod._content_signature_excluding_updated(text)
        _seed_rows(server, PROJECT_KG, fp, content_hash, [text], total_chunks=1)

        outcome, out = self._sync_node(mod, server, node)
        self.assertEqual(outcome.status, mod.OUTCOME_SYNCED, out)
        rows = _rows_for(server, PROJECT_KG, fp)
        self.assertEqual([r["chunk_num"] for r in rows], [1, 2])
        self.assertNotIn(text, [r["content"] for r in rows])

    def test_a_failed_doc_embed_leaves_the_doc_rows_in_place(self):
        doc = self.root / "docs" / "guide.md"
        text = _write_doc(doc, BODY_SINGLE_TO_MULTI)
        _seed_pending_ledger(self.root)
        mod = _load_sync_module(self.root)
        server = _FailingServer(ok=1)
        fp = "docs/guide.md"
        content_hash = mod._content_signature_excluding_updated(text)
        _seed_rows(server, DEV_COLL, fp, content_hash, [text], total_chunks=1,
                   vectors=True)

        outcome, out = self._sync_doc(mod, server, doc)
        self.assertEqual(outcome.status, mod.OUTCOME_FAILED, out)
        self.assertEqual([r["content"] for r in _rows_for(server, DEV_COLL, fp)],
                         [text])


class CodeGraphHalfTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.folder = Path(self._tmp.name)

    def test_a_completed_whole_repo_rewalk_records_the_half(self):
        from vco_lib import chunker_revision as cr
        from vco_lib import codegraph_deferrals as cd

        cd.record_successful_walk(self.folder, False, False)
        self.assertNotIn("codegraph", cr.read_resync_stamps(self.folder) or {})
        cd.record_successful_walk(self.folder, False, True)
        self.assertEqual(
            (cr.read_resync_stamps(self.folder) or {}).get("codegraph"),
            cr.current_revision(),
        )

    def test_the_printed_remedy_never_drops_the_code_graph(self):
        from vco_lib import chunker_revision as cr

        block = cr.resync_commands(Path("/proj"), "# tail")
        self.assertIn("--from-resolver --force-rewalk", block)
        self.assertNotIn("--force-recreate", block)

    def test_the_0_2_106_rung_is_a_bump_with_its_reason(self):
        from vco_lib import codegraph_extractor_generation as gen

        self.assertIn("0.2.106", gen.EXTRACTOR_GENERATION_BUMPS)
        self.assertNotIn("0.2.106", gen.EXTRACTOR_GENERATION_NON_BUMPS)
        self.assertIn("0.2.106", gen.EXTRACTOR_GENERATION_RUNG_REASONS)
        self.assertEqual(gen.CURRENT_EXTRACTOR_GENERATION, "0.2.106")
        # A graph built by 0.2.105 owes the re-walk that re-chunks it.
        self.assertFalse(gen.generation_is_current("0.2.105"))


if __name__ == "__main__":
    unittest.main()
