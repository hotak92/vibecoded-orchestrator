# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The RL training export (``rl_reranker.export``) over a synthetic launcher DB.

Covers: nothing textual leaves the DB (titles, query text, project names,
paths, ids); fixture projects and project-less rows are never exported;
--project / --exclude-project; the --list-projects preview writes nothing;
the trainer's pairing + soft-label rules; last-row-wins dedupe; the arctic
twin trust floor (v0.2.101 marker task types); the DB is opened read-only.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import numpy as np
import pytest

from rl_reranker.training import export as ex

DIM = 1024
DAY = 86_400_000
T0 = ex.V0_2_101_RELEASE_MS + 10 * DAY   # well after the v0.2.101 release
SECRET_WORDS = ("Secret Title", "private query text", "ClientProject", "/home/someone/clientproject",
                "proj-client-uuid", "task-")


def _vec(seed: int) -> list[float]:
    rng = np.random.default_rng(seed)
    v = rng.normal(size=DIM).astype(np.float32)
    return (v / np.linalg.norm(v)).round(6).tolist()


def _retrieval(tid: str, source: str, n_nodes: int, seed: int, *, linked: bool = False) -> dict:
    nodes = []
    for i in range(n_nodes):
        node = {"title": f"Secret Title {i}", "score": 0.9 - i * 0.05, "tier": "top_k",
                "emb": _vec(seed * 100 + i), "node_type": "concept" if i % 2 else "pattern",
                "cos_qn": 0.5, "shown_rank": i}
        if linked:
            node["linked_embs"] = [_vec(seed * 100 + 50 + i)]
            node["linked_type_names"] = ["tool"]
        nodes.append(node)
    return {"event": "retrieval", "schema_version": 4, "task_id": tid,
            "query": "private query text", "embedding_source": source,
            "embedding_dim": DIM, "nodes": nodes, "query_emb": _vec(seed)}


def _citation(tid: str, n_nodes: int, *, soft: bool = False, window: int = 3000) -> dict:
    titles = [f"Secret Title {i}" for i in range(n_nodes)]
    ev = {"event": "citation", "schema_version": 4, "task_id": tid,
          "citations": {t: i % 2 == 0 for i, t in enumerate(titles)},
          "cosine_sims": {t: 0.8 - 0.1 * i for i, t in enumerate(titles)},
          "literal_cited": {t: i == 0 for i, t in enumerate(titles)},
          "window_tokens": window}
    if soft:
        ev["soft_label"] = True
    return ev


class _DB:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.executescript("""
            CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT NOT NULL,
                folder_path TEXT NOT NULL UNIQUE, host TEXT NOT NULL,
                created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, slug TEXT);
            CREATE TABLE rl_events (id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL, schema_version INTEGER NOT NULL DEFAULT 3,
                ts INTEGER NOT NULL, project_id TEXT, project_name TEXT,
                task_id TEXT NOT NULL, task_type TEXT, embedding_source TEXT,
                embedding_dim INTEGER, embedding_model TEXT, payload_json TEXT NOT NULL,
                quarantined_at INTEGER, quarantine_reason TEXT);
            CREATE INDEX idx_rl_events_project_ts ON rl_events(project_id, ts);
        """)

    def project(self, pid: str, name: str) -> None:
        self.conn.execute("INSERT INTO projects VALUES (?,?,?,?,?,?,?)",
                          (pid, name, f"/home/someone/{name.lower()}", "base", 0, 0, None))

    def event(self, pid, etype, ts, tid, source, payload, task_type="pre_bash_kg_search",
              pname=None):
        self.conn.execute(
            "INSERT INTO rl_events (event_type, ts, project_id, project_name, task_id, task_type,"
            " embedding_source, embedding_dim, payload_json) VALUES (?,?,?,?,?,?,?,?,?)",
            (etype, ts, pid, pname, tid, task_type, source, DIM, json.dumps(payload)))

    def pair(self, pid, ts, tid, source, n=4, seed=1, **kw):
        cit_kw = {k: kw.pop(k) for k in ("soft", "window") if k in kw}
        self.event(pid, "retrieval", ts, tid, source, _retrieval(tid, source, n, seed, **kw))
        self.event(pid, "citation", ts + 1000, tid, source, _citation(tid, n, **cit_kw))

    def close(self):
        self.conn.commit()
        self.conn.close()


@pytest.fixture()
def db(tmp_path) -> Path:
    d = _DB(tmp_path / "launcher.db")
    d.project("proj-client-uuid", "ClientProject")
    d.project("proj-other-uuid", "OtherProject")
    d.project("proj-foo-uuid", "FooKnowledgeGraph")  # fixture-named
    # v0.2.101 marker for the client project at T0 (a twin before it is untrusted)
    d.event("proj-client-uuid", "retrieval", T0, "marker", "qwen3",
            _retrieval("marker", "qwen3", 1, 99), task_type="pre_read_kg_search")
    d.pair("proj-client-uuid", T0 + 1 * DAY, "task-a", "qwen3", seed=1, linked=True)
    d.pair("proj-client-uuid", T0 + 1 * DAY, "task-a:arctic", "arctic", seed=2)
    d.pair("proj-client-uuid", T0 - 1 * DAY, "task-early:arctic", "arctic", seed=3)  # before marker
    d.pair("proj-client-uuid", T0 + 2 * DAY, "task-soft", "qwen3", seed=4, soft=True, window=200)
    d.pair("proj-client-uuid", T0 + 2 * DAY, "task-softok", "qwen3", seed=5, soft=True, window=4000)
    d.event("proj-client-uuid", "retrieval", T0 + 3 * DAY, "task-nocit", "qwen3",
            _retrieval("task-nocit", "qwen3", 3, 6))
    # duplicate retrieval: the later row (3 nodes) must win over the earlier (5 nodes)
    d.event("proj-client-uuid", "retrieval", T0 + 4 * DAY, "task-dup", "qwen3",
            _retrieval("task-dup", "qwen3", 5, 7))
    d.event("proj-client-uuid", "retrieval", T0 + 4 * DAY + 5, "task-dup", "qwen3",
            _retrieval("task-dup", "qwen3", 3, 8))
    d.event("proj-client-uuid", "citation", T0 + 4 * DAY + 9, "task-dup", "qwen3",
            _citation("task-dup", 5))
    d.pair("proj-other-uuid", T0 + 1 * DAY, "task-o", "qwen3", seed=10)
    d.pair("proj-other-uuid", T0 + 1 * DAY, "task-o:arctic", "arctic", seed=11)  # no marker
    d.pair("proj-foo-uuid", T0 + 1 * DAY, "task-f", "qwen3", seed=12)
    d.pair(None, T0 + 1 * DAY, "task-null", "qwen3", seed=13)
    d.close()
    return tmp_path / "launcher.db"


SALT = b"s" * 64


def _all_output_bytes(out: Path) -> bytes:
    blob = b""
    for f in sorted(out.rglob("*")):
        if f.is_file() and f.name != "FIELDS.md":
            blob += f.read_bytes()
            if f.suffix == ".npz":
                with np.load(f) as z:
                    for k in z.files:
                        assert z[k].dtype.kind in "iuf", (f, k, z[k].dtype)
                        blob += z[k].tobytes()
    return blob


def test_export_is_text_free_and_applies_the_rules(db, tmp_path):
    out = tmp_path / "out"
    idx = ex.run_export(db, out, salt=SALT, include=[], exclude=[])
    blob = _all_output_bytes(out)
    for word in SECRET_WORDS + ("OtherProject", "proj-other-uuid", "pattern", "FooKnowledge"):
        assert word.encode() not in blob, word
    assert (out / "FIELDS.md").is_file()

    q = idx["profiles"]["qwen3"]
    a = idx["profiles"]["arctic"]
    # qwen3: task-a, task-softok, task-dup (client) + task-o (other)
    assert q["queries"] == 4
    assert q["dropped"]["retrieval_soft_label_short_window"] == 1
    assert q["dropped"]["retrieval_no_citation"] == 2  # task-nocit + the marker row
    assert q["dropped"]["retrieval_duplicate_superseded"] == 1
    # arctic: only task-a:arctic (after the client's marker); the other project never ran it
    assert a["queries"] == 1
    assert a["dropped"]["retrieval_arctic_before_prefix_fix"] == 2
    # projects are hashed; the fixture project and NULL rows are absent
    hashes = idx["projects"]
    assert hashes[0] == ex.hash_project_id(SALT, "proj-client-uuid")  # most rows first
    assert ex.hash_project_id(SALT, "proj-foo-uuid") not in hashes
    assert len(hashes) == 2

    shard = ex.load_profile_shard(out, "qwen3")
    s, qq = shard.samples, shard.queries
    # dedupe: task-dup's surviving retrieval has 3 nodes -> 3 samples
    assert sorted(qq["n_samples"].tolist()) == [3, 4, 4, 4]
    # soft-label weight carried; hard = 1.0
    assert sorted(set(np.round(qq["weight"], 3).tolist())) == [0.5, 1.0]
    # linked vectors exported for task-a, typed 'tool' (builtin index 2)
    assert (s["linked_vec"][:, 0] >= 0).sum() == 4
    assert set(s["linked_type"][s["linked_vec"][:, 0] >= 0, 0].tolist()) == {2}
    # non-builtin node type -> OTHER code; builtin 'concept' -> 0
    assert set(s["node_type"].tolist()) == {0, ex.TYPE_CODE_OTHER}
    # targets follow offline_trainer.citation_targets for the same event
    from rl_reranker.offline_trainer import citation_targets
    decided = citation_targets(_citation("x", 4))
    assert decided is not None
    t, _w = decided
    first = int(qq["first_sample"][0])
    got = s["target"][first:first + 4]
    assert np.allclose(sorted(got), sorted(t.values()), atol=1e-6)
    assert shard.vectors.dtype == np.float16 and shard.vectors.shape[1] == DIM
    # timestamps are rounded to the hour
    assert np.all(qq["ts_hour_ms"] % ex.TS_GRANULARITY_MS == 0)


def test_include_and_exclude(db, tmp_path):
    idx = ex.run_export(db, tmp_path / "o1", salt=SALT, include=["OtherProject"], exclude=[])
    assert idx["projects"] == [ex.hash_project_id(SALT, "proj-other-uuid")]
    h = ex.hash_project_id(SALT, "proj-client-uuid")
    idx2 = ex.run_export(db, tmp_path / "o2", salt=SALT, include=[], exclude=[h])
    assert idx2["projects"] == [ex.hash_project_id(SALT, "proj-other-uuid")]
    with pytest.raises(ValueError):
        ex.run_export(db, tmp_path / "o3", salt=SALT, include=["NoSuchProject"], exclude=[])


def test_list_projects_previews_and_writes_nothing(db, tmp_path, capsys):
    out = tmp_path / "never"
    salt_file = tmp_path / "salt"
    rc = ex.main(["--db", str(db), "--salt-file", str(salt_file), "--list-projects",
                  "--exclude-project", "OtherProject", "--out", str(out)])
    assert rc == 0
    assert not out.exists()
    text = capsys.readouterr().out
    lines = {}
    for ln in text.splitlines():
        for name in ("ClientProject", "OtherProject", "FooKnowledgeGraph"):
            if ln.rstrip().endswith(name):
                lines[name] = ln
    assert lines["ClientProject"].split()[0] == "yes"
    assert lines["OtherProject"].split()[0] == "no"
    assert lines["FooKnowledgeGraph"].split()[0] == "fixture"
    assert "no project id" in text


def test_db_is_opened_read_only_and_left_untouched(db, tmp_path):
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    ex.run_export(db, tmp_path / "o", salt=SALT, include=[], exclude=[])
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
    conn = ex.open_db_readonly(db)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM rl_events")
    conn.close()


def test_refuses_a_non_empty_output_dir(db, tmp_path):
    out = tmp_path / "busy"
    out.mkdir()
    (out / "x").write_text("x")
    with pytest.raises(FileExistsError):
        ex.run_export(db, out, salt=SALT, include=[], exclude=[])


def test_salt_is_created_once_and_reused(tmp_path):
    p = tmp_path / "state" / "rl_export_salt"
    s1 = ex.load_or_create_salt(p)
    s2 = ex.load_or_create_salt(p)
    assert s1 == s2 and len(s1) >= 32
    assert ex.hash_project_id(s1, "x") != ex.hash_project_id(b"other" * 10, "x")


@pytest.mark.parametrize("name,expected", [
    ("Foo", True), ("FooKnowledgeGraph", True), ("Alpha_KnowledgeGraph", True),
    ("alphaknowledgegraph", True), ("ClientProject", False), ("VibeCoded Orchestrator", False),
    ("", False), (None, False),
])
def test_fixture_project_names(name, expected):
    from vco_lib.fixture_class_guard import is_fixture_project_name
    assert is_fixture_project_name(name) is expected


def test_twin_trust_floor():
    assert ex.twin_trust_floor_ms(None) is None
    assert ex.twin_trust_floor_ms(ex.V0_2_101_RELEASE_MS - DAY) == ex.V0_2_101_RELEASE_MS
    assert ex.twin_trust_floor_ms(T0) == T0
