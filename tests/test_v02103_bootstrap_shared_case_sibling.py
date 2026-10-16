# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.103 R24 HIGH-1 — `bootstrap_collections` never destroys the root's
shared KG.

Since v0.2.103 the orchestrator root owns its FOLDER-DERIVED KG name
(``vco_lib.root_kg_identity``); on a ``vibecoded-orchestrator`` clone that is
``VibecodedOrchestrator_KnowledgeGraph``, a case-only sibling of the shipped
constant ``VibeCodedOrchestrator_KnowledgeGraph``. The per-project bootstrap
(launcher project add / install-bundle) used to target the constant, meet
Weaviate's 422 "found similar class", and DROP the sibling — the root's
populated shared KG — to recreate it empty.

Now: (1) the shared target is the machine's shared class
(``project_init._resolve_shared_kg_name`` → the root's binding); (2) the
case-conflict recovery never drops a sibling that holds objects or cannot be
counted — it is reported and deferred (``collection_blocked_by_case_sibling``);
an EMPTY sibling keeps the pre-existing drop-and-recreate; (3) the consented
``drop-collections`` purge refuses the machine's shared class too.

The Weaviate side is a fake at ``project_init._http_request`` (the module's own
seam), so schema probes, the 422 parse, the Aggregate count and the deletes all
run the real code. The root binding lives in a real-schema launcher.db.

Red-proofs (mutation, restored from a saved copy, md5-verified), all in
``vco_lib/bootstrap_case_sibling.py``:
  * ``shared_targets`` returns ``[project_init._SHARED_KG_NAME]`` → test 1 fails;
  * ``blocks`` never blocks (``if rows == 0`` → ``if True``) → tests 2 and 3 fail;
  * ``blocks`` always blocks (``if rows == 0`` → ``if False``) → test 4 fails;
  * ``protected_shared_names`` returns only the constant → test 5 fails;
and in ``vco_lib/weaviate_schema.py`` dropping the machine-shared candidate →
test 6 fails.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3

import pytest

from vco_lib import project_init

DERIVED = "VibecodedOrchestrator_KnowledgeGraph"
CONST = "VibeCodedOrchestrator_KnowledgeGraph"
CID = "collection_blocked_by_case_sibling"


class _FakeWeaviate:
    """Case-sensitive GETs, case-insensitive class identity on POST (422
    "found similar class"), Aggregate counts, recorded deletes."""

    def __init__(self, counts, *, count_fails=False):
        self.counts = dict(counts)
        self.count_fails = count_fails
        self.deleted: list = []
        self.created: list = []

    def __call__(self, method, url, *, body=None, timeout=30.0):
        path = url.split("://", 1)[-1].split("/", 1)[-1]
        if method == "GET" and path.startswith("v1/schema/"):
            name = path[len("v1/schema/"):]
            if name in self.counts:
                return 200, json.dumps(project_init.kg_class_definition(name)).encode()
            return 404, b"{}"
        if method == "POST" and path == "v1/schema":
            name = (body or {}).get("class")
            for live in self.counts:
                if live.lower() == name.lower():
                    msg = ("class already exists" if live == name else
                           f'class already exists: found similar class "{live}"')
                    return 422, json.dumps({"error": [{"message": msg}]}).encode()
            self.counts[name] = 0
            self.created.append(name)
            return 200, b"{}"
        if method == "DELETE" and path.startswith("v1/schema/"):
            name = path[len("v1/schema/"):]
            self.deleted.append(name)
            self.counts.pop(name, None)
            return 200, b""
        if method == "POST" and path == "v1/graphql":
            if self.count_fails:
                return 500, b"boom"
            name = re.search(r"Aggregate \{ (\S+) \{", body["query"]).group(1)
            return 200, json.dumps({"data": {"Aggregate": {
                name: [{"meta": {"count": self.counts.get(name, 0)}}]}}}).encode()
        if method == "GET" and path.startswith("v1/objects"):
            return 200, b'{"objects": []}'
        return 404, b""


@pytest.fixture
def machine(monkeypatch, tmp_path):
    """A launcher.db with an orchestrator-root project, and a fresh project folder."""
    from tests.common.launcher_db_fixture import create_empty_launcher_db

    db = tmp_path / "launcher.db"
    create_empty_launcher_db(db)
    monkeypatch.setenv("VCT_LAUNCHER_DB_PATH", str(db))
    monkeypatch.setattr(project_init, "_is_weaviate_reachable", lambda url, timeout=5.0: True)
    for k in ("KG_COLLECTION", "SHARED_KG_COLLECTION", "DEVELOPMENT_COLLECTION"):
        monkeypatch.delenv(k, raising=False)
    folder = tmp_path / "acme"
    folder.mkdir()

    def bind_root(name):
        conn = sqlite3.connect(str(db))
        try:
            conn.execute("INSERT INTO projects (id, name, folder_path, host, slug, created_at, "
                         "updated_at) VALUES ('root', 'VCO', ?, 'orchestrator_root', "
                         "'orchestrator-root', 0, 0)", (str(tmp_path / "vibecoded-orchestrator"),))
            for role in ("primary", "shared"):
                conn.execute("INSERT INTO project_kg_bindings (project_id, role, collection_name, "
                             "config_json, updated_at) VALUES ('root', ?, ?, '{}', 0)", (role, name))
            conn.commit()
        finally:
            conn.close()

    return folder, bind_root


def _bootstrap(monkeypatch, fake, folder):
    monkeypatch.setattr(project_init, "_http_request", fake)
    return project_init.bootstrap_collections(
        "Acme", weaviate_url="http://weaviate.test", project_folder=folder)


def _ledger(folder) -> str:
    p = folder / ".claude" / "context" / "UPDATE_DEFERRED.md"
    return p.read_text(encoding="utf-8") if p.is_file() else ""


def test_project_add_binds_the_roots_folder_derived_shared_kg_and_never_drops_it(machine, monkeypatch):
    """The root bound its folder-derived class (892 rows): a project add
    targets THAT class as the shared KG, leaves it intact, and creates no
    constant-named sibling; the projection binds the project to it."""
    folder, bind_root = machine
    bind_root(DERIVED)
    fake = _FakeWeaviate({DERIVED: 892})
    result = _bootstrap(monkeypatch, fake, folder)
    assert fake.deleted == []
    assert fake.counts[DERIVED] == 892
    assert CONST not in fake.created
    assert {"collection": DERIVED, "action": "exists", "ok": True} in result["actions"]
    assert result["errors"] == [] and result["blocked"] == [] and result["regenerated"] == []
    assert project_init._resolve_shared_kg_name(folder) == DERIVED
    assert CID not in _ledger(folder)


def test_a_populated_case_sibling_is_never_dropped_and_is_deferred(machine, monkeypatch):
    """No root binding resolves (last-resort constant), and Weaviate holds the
    populated folder-derived class: the 422 sibling is NOT dropped, nothing is
    created under the constant, the block is reported and deferred with the
    consent command. Once the sibling is gone the next run creates the class
    and clears the row (paired resolution)."""
    folder, _bind_root = machine
    fake = _FakeWeaviate({DERIVED: 892})
    result = _bootstrap(monkeypatch, fake, folder)
    assert fake.deleted == [] and fake.counts[DERIVED] == 892
    assert CONST not in fake.counts
    assert result["blocked"] == [{"collection": CONST, "sibling": DERIVED, "object_count": 892}]
    assert {"collection": CONST, "action": "blocked", "ok": False} in result["actions"]
    assert any(e["collection"] == CONST and DERIVED in e["error"] for e in result["errors"])
    ledger = _ledger(folder)
    assert CID in ledger
    assert f"python -m vco_lib.root_kg_identity adopt --collection {DERIVED} --yes" in ledger

    fake.counts.pop(DERIVED)  # the user removed (or adopted elsewhere) the sibling
    second = _bootstrap(monkeypatch, fake, folder)
    assert CONST in fake.created and second["blocked"] == []
    assert CID not in _ledger(folder)


def test_an_uncountable_case_sibling_is_never_dropped(machine, monkeypatch):
    """A sibling whose object count cannot be read is treated as populated."""
    folder, _bind_root = machine
    fake = _FakeWeaviate({DERIVED: 892}, count_fails=True)
    result = _bootstrap(monkeypatch, fake, folder)
    assert fake.deleted == [] and DERIVED in fake.counts
    assert result["blocked"] == [{"collection": CONST, "sibling": DERIVED, "object_count": None}]


def test_an_empty_stale_case_sibling_keeps_the_drop_and_recreate(machine, monkeypatch):
    """Pre-existing behaviour for an EMPTY sibling (nothing to lose): dropped,
    the target created under its own name, reported as a case-conflict regen."""
    folder, _bind_root = machine
    fake = _FakeWeaviate({DERIVED: 0})
    result = _bootstrap(monkeypatch, fake, folder)
    assert fake.deleted == [DERIVED]
    assert CONST in fake.counts and DERIVED not in fake.counts
    regen = [r for r in result["regenerated"] if r["collection"] == CONST]
    assert regen and regen[0]["reason"] == "case-conflict" and regen[0]["dropped_name"] == DERIVED
    assert result["blocked"] == [] and result["errors"] == []


def test_drop_collections_never_purges_the_machines_shared_class(machine, monkeypatch, capsys):
    """A project whose derived KG name is (in any casing) the root's shared
    class — here a root bound to ``VcoDev_KnowledgeGraph`` and a project named
    ``VCODev`` — is refused by the consented purge, which still drops the
    project's other classes. The constant alone would not protect it."""
    folder, bind_root = machine
    bind_root("VcoDev_KnowledgeGraph")
    fake = _FakeWeaviate({"VcoDev_KnowledgeGraph": 892, "VCODev_Development": 3})
    monkeypatch.setattr(project_init, "_http_request", fake)
    rc = project_init._cmd_drop_collections(argparse.Namespace(
        name="VCODev", weaviate_url="http://weaviate.test", json=True))
    assert "VCODev_KnowledgeGraph" not in fake.deleted
    assert fake.counts["VcoDev_KnowledgeGraph"] == 892
    assert "VCODev_Development" in fake.deleted
    assert rc == 1  # the refusal is reported as an error entry
    out = json.loads(capsys.readouterr().out)
    assert out["skipped_shared"] == "VcoDev_KnowledgeGraph"
    assert any(e["collection"] == "VCODev_KnowledgeGraph" and "refusing" in e["error"]
               for e in out["errors"])


def test_a_per_project_migrate_enumerates_the_machines_shared_class(machine, monkeypatch):
    """`enumerate_kg_collections(project_name=...)` (the per-project migrate's
    target list) includes the root's folder-derived shared class, not only the
    constant. Red-proof: drop the machine-shared candidate → fails."""
    from vco_lib import weaviate_schema

    _folder, bind_root = machine
    bind_root(DERIVED)
    monkeypatch.setattr(weaviate_schema, "_list_all_classes",
                        lambda weaviate_url=None: ["Acme_KnowledgeGraph", DERIVED])
    got = weaviate_schema.enumerate_kg_collections(project_name="Acme",
                                                   weaviate_url="http://weaviate.test")
    assert got == ["Acme_KnowledgeGraph", DERIVED]
