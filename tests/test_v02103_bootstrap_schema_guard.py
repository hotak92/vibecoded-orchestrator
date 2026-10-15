# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.103 — the collection bootstrap never drops a POPULATED class to repair
its schema (``vco_lib.bootstrap_schema_guard``).

``bootstrap_collections`` used to drop + recreate every existing class whose
schema ``_schema_incompatible`` flags. Now: a class that holds objects (or
cannot be counted) is left untouched and gets the existing
``schema_migration_required`` deferral naming ``migrate-collections``; an
EMPTY class keeps the drop-and-recreate. Every class — the shared KG too, whose
entry goes to the orchestrator root's ledger with ``--name <root>`` (the only
``migrate-collections`` scope that rebuilds it).

Same fake as the HIGH-1 tests (``project_init._http_request`` seam, real-schema
launcher.db), extended to serve a stale schema for chosen classes.

Red-proofs (mutation, restored from a saved copy, md5-verified):
  * ``defers`` never defers (``if rows == 0`` → ``if True``) → tests 1, 2, 4, 5, 6 fail;
  * ``defers`` always defers (``if rows == 0`` → ``if False``) → test 3 fails;
  * the ``_bsg.defers`` call removed from ``project_init`` → tests 1, 2, 4, 5, 6 fail;
  * ``settle`` routes the shared class to the project (owner check → always own) → test 4 fails;
  * the no-root key passes the project folder as ``--project-folder`` → test 7 fails.
"""
from __future__ import annotations

import json

from tests import test_v02103_bootstrap_shared_case_sibling as _cs
from vco_lib import project_init

DERIVED, _FakeWeaviate = _cs.DERIVED, _cs._FakeWeaviate
machine = _cs.machine  # the HIGH-1 tests' launcher.db + project-folder fixture

CID = "schema_migration_required"
KG = "Acme_KnowledgeGraph"


def _legacy(name):
    """Pre-v0.2.17 single-vector schema → `migrate-collections` action `rebuild`."""
    return {"class": name, "invertedIndexConfig": {"indexNullState": False}, "properties": []}


def _missing_core_slot(name):
    """A current schema missing the `qwen3_embed` slot → action `copy` (lossless)."""
    d = project_init.kg_class_definition(name)
    d["vectorConfig"] = {k: v for k, v in d["vectorConfig"].items() if k != "qwen3_embed"}
    return d


class _StaleWeaviate(_FakeWeaviate):
    def __init__(self, counts, stale, **kw):
        super().__init__(counts, **kw)
        self.stale = dict(stale)

    def __call__(self, method, url, *, body=None, timeout=30.0):
        path = url.split("://", 1)[-1].split("/", 1)[-1]
        if method == "GET" and path.startswith("v1/schema/"):
            name = path[len("v1/schema/"):]
            if name in self.stale and name in self.counts:
                return 200, json.dumps(self.stale[name](name)).encode()
        if method == "DELETE" and path.startswith("v1/schema/"):
            self.stale.pop(path[len("v1/schema/"):], None)
        return super().__call__(method, url, body=body, timeout=timeout)


def _run(monkeypatch, fake, folder, *, dry_run=False):
    monkeypatch.setattr(project_init, "_http_request", fake)
    return project_init.bootstrap_collections(
        "Acme", weaviate_url="http://weaviate.test", project_folder=folder,
        kg_only=True, dry_run=dry_run)


def _ledger(folder) -> str:
    p = folder / ".claude" / "context" / "UPDATE_DEFERRED.md"
    return p.read_text(encoding="utf-8") if p.is_file() else ""


def test_a_populated_stale_project_kg_is_never_dropped_and_is_deferred(machine, monkeypatch):
    folder, _bind_root = machine
    fake = _StaleWeaviate({KG: 50}, {KG: _legacy})
    result = _run(monkeypatch, fake, folder)
    assert fake.deleted == [] and fake.counts[KG] == 50
    assert result["regenerated"] == []
    assert {"collection": KG, "action": "deferred", "ok": True} in result["actions"]
    (d,) = result["schema_deferred"]
    assert (d["collection"], d["object_count"], d["migrate_action"]) == (KG, 50, "rebuild")
    ledger = _ledger(folder)
    assert CID in ledger and "collection bootstrap found" in ledger
    assert "migrate-collections --name 'Acme'" in ledger
    assert f"--project-folder {str(folder)!r}" in ledger


def test_an_uncountable_stale_class_is_never_dropped(machine, monkeypatch):
    folder, _bind_root = machine
    fake = _StaleWeaviate({KG: 50}, {KG: _legacy}, count_fails=True)
    result = _run(monkeypatch, fake, folder)
    assert fake.deleted == []
    assert result["schema_deferred"][0]["object_count"] is None
    assert CID in _ledger(folder)


def test_an_empty_stale_class_keeps_the_drop_and_recreate(machine, monkeypatch):
    folder, _bind_root = machine
    fake = _StaleWeaviate({KG: 0}, {KG: _legacy})
    result = _run(monkeypatch, fake, folder)
    assert fake.deleted == [KG] and KG in fake.counts
    assert [r["collection"] for r in result["regenerated"]] == [KG]
    assert result["schema_deferred"] == []
    assert CID not in _ledger(folder)


def test_a_populated_stale_shared_kg_is_deferred_to_the_roots_ledger(machine, monkeypatch, tmp_path):
    """The shared class (the root's KG) is never dropped by a project's
    bootstrap; its entry goes to the ROOT's ledger with `--name <root>` —
    `migrate-collections --name Acme` would never rebuild it, and Acme's next
    clean dry-run would clear the row while the shared class is still stale."""
    folder, bind_root = machine
    bind_root(DERIVED)
    root = tmp_path / "vibecoded-orchestrator"
    root.mkdir()
    fake = _StaleWeaviate({KG: 0, DERIVED: 892}, {DERIVED: _legacy})
    result = _run(monkeypatch, fake, folder)
    assert DERIVED not in fake.deleted and fake.counts[DERIVED] == 892
    assert [d["collection"] for d in result["schema_deferred"]] == [DERIVED]
    root_ledger = _ledger(root)
    assert CID in root_ledger and DERIVED in root_ledger
    assert "migrate-collections --name 'VCO'" in root_ledger
    assert f"--project-folder {str(root)!r}" in root_ledger
    assert CID not in _ledger(folder)


def test_a_missing_slot_is_labelled_a_lossless_copy(machine, monkeypatch):
    folder, _bind_root = machine
    fake = _StaleWeaviate({KG: 7}, {KG: _missing_core_slot})
    result = _run(monkeypatch, fake, folder)
    assert fake.deleted == []
    assert result["schema_deferred"][0]["migrate_action"] == "copy"
    assert "**copy** (lossless: vectors preserved)" in _ledger(folder)


def test_dry_run_reports_would_defer_and_writes_nothing(machine, monkeypatch):
    folder, _bind_root = machine
    fake = _StaleWeaviate({KG: 50}, {KG: _legacy})
    result = _run(monkeypatch, fake, folder, dry_run=True)
    assert fake.deleted == [] and fake.created == []
    assert {"collection": KG, "action": "would-defer", "ok": True} in result["actions"]
    assert result["regenerated"] == []
    assert _ledger(folder) == ""


def test_with_no_registered_root_the_shared_entry_names_the_class_stem(machine, monkeypatch):
    """No root project → no known owner: the entry stays in the project's
    ledger, `--name` is the class stem (whose derived KG IS that class) and no
    `--project-folder` is given (nothing to re-ingest from — the project's own
    knowledge must not be synced into the shared class)."""
    folder, _bind_root = machine
    const = project_init._SHARED_KG_NAME
    fake = _StaleWeaviate({KG: 0, const: 30}, {const: _legacy})
    result = _run(monkeypatch, fake, folder)
    assert const not in fake.deleted
    ledger = _ledger(folder)
    assert "migrate-collections --name 'VibeCodedOrchestrator' --weaviate-url 'http://weaviate.test' --json" in ledger
    assert "--project-folder" not in ledger
    assert [d["collection"] for d in result["schema_deferred"]] == [const]
