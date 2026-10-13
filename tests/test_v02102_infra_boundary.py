# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 W5-A — the infrastructure boundary with a project that runs its own
Weaviate / Ollama (M3), the compose override pair (P299-B2) and the
binding-backed identity of the doctor's detectors.

Every gating decision is tested on BOTH arms (declared → left alone,
undeclared → VCO acts as before); container tests use docker AND podman
shapes. The "other project" is ``peer-project`` throughout.
"""
from __future__ import annotations

import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.common.launcher_db_fixture import (  # noqa: E402
    add_project,
    create_empty_launcher_db,
    insert_rows,
    make_launcher_db,
)
from vco_lib import compose_override_pair as cop  # noqa: E402
from vco_lib import infra_declarations as infra  # noqa: E402

PEER = "peer-project"


def _declare(db: Path, project_id: str, kind: str, *values: str) -> None:
    insert_rows(db, infra.TABLE, [{"project_id": project_id, "kind": kind, "value": v} for v in values])


def _machine(tmp: Path) -> Path:
    db = make_launcher_db(tmp)
    add_project(db, project_id="p-peer", name=PEER, folder_path=tmp / "peer-folder",
                kg_primary="PeerOne_KnowledgeGraph")
    return db


# ---------------------------------------------------------------------------
# The one reader
# ---------------------------------------------------------------------------

#: The SAME cases as the Rust ``normalise_matches_the_python_rule`` and the
#: vitest table in ``infra-declarations.test.ts`` — three validators, one rule.
NORMALISE_CASES = [
    (infra.KIND_EXTERNAL_COLLECTION, " peerRuntime_Memory ", "PeerRuntime_Memory"),
    (infra.KIND_EXTERNAL_COLLECTION, "1Bad", None),
    (infra.KIND_EXTERNAL_COLLECTION, "has-dash", None),
    (infra.KIND_UNMANAGED_CONTAINER, "peer-weaviate.1", "peer-weaviate.1"),
    (infra.KIND_UNMANAGED_CONTAINER, "-lead", None),
    (infra.KIND_UNMANAGED_CONTAINER, "a b", None),
    (infra.KIND_LOCAL_PORT, "011436", "11436"),
    (infra.KIND_LOCAL_PORT, "0", None),
    (infra.KIND_LOCAL_PORT, "65536", None),
    (infra.KIND_LOCAL_PORT, "+80", None),
    (infra.KIND_UNMANAGED_CONTAINER, "", None),
]


@pytest.mark.parametrize("kind,raw,want", NORMALISE_CASES)
def test_normalise_parity_table(kind, raw, want):
    assert infra.normalise(kind, raw) == want


def test_the_parity_table_is_the_one_the_rust_and_ts_tests_carry():
    """The three tables are written out in three languages; this pins that
    each case here appears verbatim in the other two (so editing one table
    without the others fails)."""
    rust = (REPO_ROOT / "launcher/src-tauri/vct-launcher-core/src/db/infra_declarations.rs").read_text()
    ts = (REPO_ROOT / "launcher/src/lib/project-state/infra-declarations.test.ts").read_text()
    for _kind, raw, want in NORMALISE_CASES:
        # Rust literals are double-quoted, the TS ones single-quoted.
        assert json.dumps(raw) in rust and f"'{raw}'" in ts, raw
        if want is not None:
            assert json.dumps(want) in rust and f"'{want}'" in ts, want


def test_unicode_digits_are_not_a_port():
    assert infra.normalise(infra.KIND_LOCAL_PORT, "١٢") is None


def test_load_unions_db_and_env_and_names_the_declarer(tmp_path):
    db = _machine(tmp_path)
    _declare(db, "p-peer", infra.KIND_UNMANAGED_CONTAINER, "peer-weaviate")
    decl = infra.load(db_path=db, env={infra.ENV_EXTERNAL_COLLECTIONS: "peerRuntime_Dialogue, bad-name"})
    assert decl.resolvable
    assert decl.values(infra.KIND_UNMANAGED_CONTAINER) == {"peer-weaviate"}
    assert decl.values(infra.KIND_EXTERNAL_COLLECTION) == {"PeerRuntime_Dialogue"}
    assert decl.why(infra.KIND_UNMANAGED_CONTAINER, "peer-weaviate") == \
        f"declared unmanaged by project '{PEER}'"
    assert "env:VCT_EXTERNAL_COLLECTIONS" in decl.why(infra.KIND_EXTERNAL_COLLECTION, "PeerRuntime_Dialogue")


def test_load_without_a_db_is_resolvable_and_an_unreadable_db_is_not(tmp_path):
    assert infra.load(db_path=tmp_path / "absent.db", env={}).resolvable
    bad = tmp_path / "launcher.db"
    bad.write_bytes(b"this is not sqlite" * 100)
    assert not infra.load(db_path=bad, env={}).resolvable
    # A launcher.db older than migration 049 simply has nothing declared.
    old = create_empty_launcher_db(tmp_path / "old.db", up_to=48)
    decl = infra.load(db_path=old, env={})
    assert decl.resolvable and not decl.rows


# ---------------------------------------------------------------------------
# M3-2: the env projection carries the declarations (and drops them again)
# ---------------------------------------------------------------------------

def test_projection_emits_each_declared_kind_and_omits_empty_ones(tmp_path):
    from vco_lib.config_projection import list_canonical_keys, project_env_from_db

    db = _machine(tmp_path)
    env = project_env_from_db("p-peer", db_path=db)["canonical_env"]
    for key in infra.KIND_ENV.values():
        assert key in list_canonical_keys()
        assert key not in env, f"{key} emitted with nothing declared"
    _declare(db, "p-peer", infra.KIND_LOCAL_PORT, "11436", "8082")
    _declare(db, "p-peer", infra.KIND_EXTERNAL_COLLECTION, "PeerRuntime_Memory")
    env = project_env_from_db("p-peer", db_path=db)["canonical_env"]
    assert env["VCT_SSRF_EXTRA_LOCAL_PORTS"] == "8082,11436"
    assert env["VCT_EXTERNAL_COLLECTIONS"] == "PeerRuntime_Memory"
    assert "VCT_UNMANAGED_CONTAINERS" not in env


# ---------------------------------------------------------------------------
# M3-5: the SSRF guard — a declared extra local port, never a literal
# ---------------------------------------------------------------------------

def _verdict(url: str, **env: str) -> str:
    from vco_lib import ssrf_url

    old = {k: os.environ.get(k) for k in env}
    try:
        os.environ.update(env)
        return ssrf_url.verdict(url)
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_ssrf_port_11436_blocked_until_declared_and_undeclared_stays_blocked(monkeypatch):
    monkeypatch.delenv(infra.ENV_SSRF_EXTRA_LOCAL_PORTS, raising=False)
    assert _verdict("http://localhost:11436/api/tags") == "block"
    assert _verdict("http://localhost:11436/api/tags", VCT_SSRF_EXTRA_LOCAL_PORTS="11436") == "allow"
    assert _verdict("http://127.0.0.1:11436/", VCT_SSRF_EXTRA_LOCAL_PORTS="11436, 9") == "allow"
    assert _verdict("http://[::1]:11436/", VCT_SSRF_EXTRA_LOCAL_PORTS="11436") == "allow"
    # Another port stays blocked; junk entries are ignored, never widened.
    assert _verdict("http://localhost:11437/", VCT_SSRF_EXTRA_LOCAL_PORTS="11436") == "block"
    assert _verdict("http://localhost:11436/", VCT_SSRF_EXTRA_LOCAL_PORTS="abc,70000,-1") == "block"
    # A declared port is a LOOPBACK allowance only.
    assert _verdict("http://10.0.0.5:11436/", VCT_SSRF_EXTRA_LOCAL_PORTS="11436") == "block"


# ---------------------------------------------------------------------------
# M3-1/M3-2: external collections leave the binding-evidence scan
# ---------------------------------------------------------------------------

class _Machine:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.db = make_launcher_db(tmp)
        self.classes: dict[str, tuple[int, tuple[str, ...]]] = {}

    def project(self, pid, name, folder_name, kg_primary, files=()):
        folder = self.tmp / folder_name
        for rel in files:
            (folder / rel).parent.mkdir(parents=True, exist_ok=True)
            (folder / rel).write_text("x", encoding="utf-8")
        add_project(self.db, project_id=pid, name=name, folder_path=folder, kg_primary=kg_primary)
        return folder

    def cls(self, name, paths):
        self.classes[name] = (len(paths), tuple(paths))

    def scan(self, **kw):
        from vco_lib import kg_binding_doctor as kbd

        return kbd.scan_kg_binding_evidence(
            db_path=self.db,
            list_classes=lambda: list(self.classes),
            count_objects=lambda c: self.classes[c][0],
            sample_paths=lambda c: self.classes[c][1],
            **kw,
        )


def _peer_runtime_machine(tmp: Path) -> _Machine:
    m = _Machine(tmp)
    files = [f"knowledge/n{i}.md" for i in range(3)]
    m.project("p-peer", PEER, "peer-folder", "PeerOne_KnowledgeGraph", files)
    m.cls("PeerOne_KnowledgeGraph", files)
    # The project's own runtime class, VCO-shaped by name, its paths anchored
    # nowhere VCO knows: exactly what the unclaimed reading flags.
    m.cls("PeerTwo_KnowledgeGraph", [f"runtime/r{i}.md" for i in range(5)])
    return m


def test_undeclared_runtime_class_is_unclaimed_and_declared_one_is_skipped(tmp_path):
    m = _peer_runtime_machine(tmp_path)
    scan = m.scan()
    assert [u.name for u in scan.unclaimed] == ["PeerTwo_KnowledgeGraph"]
    assert scan.external == ()
    _declare(m.db, "p-peer", infra.KIND_EXTERNAL_COLLECTION, "PeerTwo_KnowledgeGraph")
    scan = m.scan()
    assert scan.unclaimed == ()
    assert [(e.name, e.why) for e in scan.external] == [
        ("PeerTwo_KnowledgeGraph", f"declared external by project '{PEER}'")]


def test_a_declared_class_is_never_binding_evidence(tmp_path):
    """Evidence feeds the heal that RE-POINTS bindings — a declared class
    holding the project's paths must never become a re-point target."""
    m = _Machine(tmp_path)
    files = [f"knowledge/n{i}.md" for i in range(4)]
    m.project("p-peer", PEER, "peer-folder", "PeerOne_KnowledgeGraph", files)
    m.cls("PeerOne_KnowledgeGraph", files)
    m.cls("GhostA_KnowledgeGraph", files)  # same paths: would be a ghost
    assert m.scan().mismatches, "undeclared: the mirror is reported as a ghost"
    _declare(m.db, "p-peer", infra.KIND_EXTERNAL_COLLECTION, "GhostA_KnowledgeGraph")
    scan = m.scan()
    assert not scan.mismatches
    assert all(e.name != "GhostA_KnowledgeGraph" for v in scan.verdicts for e in v.evidence)


def test_a_declared_fixture_shaped_class_is_not_listed_as_a_ghost(tmp_path):
    m = _peer_runtime_machine(tmp_path)
    m.cls("Alpha_Diagrams", [])
    assert "Alpha_Diagrams" in m.scan().fixture_shaped
    _declare(m.db, "p-peer", infra.KIND_EXTERNAL_COLLECTION, "Alpha_Diagrams")
    assert "Alpha_Diagrams" not in m.scan().fixture_shaped


def test_unreadable_declarations_make_the_scan_unlookable(tmp_path):
    m = _peer_runtime_machine(tmp_path)
    assert m.scan(external_collections=lambda: None) is None


def test_doctor_names_the_skip_and_the_remedy_names_the_allowlist(tmp_path):
    from vco_lib import doctor

    m = _peer_runtime_machine(tmp_path)
    res = doctor.DoctorResolvers(kg_binding_evidence=m.scan)
    problem = [f for f in doctor.probe_kg_binding_evidence(tmp_path, res, {})
               if f.condition_id == doctor.CID_KG_UNCLAIMED]
    assert problem and "declare it external" in problem[0].command
    assert "Identity" in problem[0].command
    _declare(m.db, "p-peer", infra.KIND_EXTERNAL_COLLECTION, "PeerTwo_KnowledgeGraph")
    findings = doctor.probe_kg_binding_evidence(tmp_path, res, {})
    assert [f.status for f in findings] == [doctor.STATUS_OK]
    assert "Skipped 1 declared-external class(es)" in findings[0].summary
    assert PEER in findings[0].summary
    assert findings[0].detail["external"][0]["class"] == "PeerTwo_KnowledgeGraph"


def test_the_unclaimed_condition_names_the_allowlist_as_remedy():
    text = (REPO_ROOT / "vco_lib/deferral_conditions.toml").read_text(encoding="utf-8")
    block = text.split("[conditions.kg_unclaimed_populated_classes]", 1)[1].split("[conditions.", 1)[0]
    assert "external" in block and "VCT_EXTERNAL_COLLECTIONS" in block


# ---------------------------------------------------------------------------
# M3-3: binding-backed identity — a folder whose basename differs from its binding
# ---------------------------------------------------------------------------

def test_binding_evidence_identity_comes_from_the_binding_not_the_basename(tmp_path):
    m = _Machine(tmp_path)
    files = [f"knowledge/n{i}.md" for i in range(3)]
    # Folder basename "client-a-checkout" names neither the project nor its class.
    m.project("p-peer", PEER, "client-a-checkout", "PeerOne_KnowledgeGraph", files)
    m.cls("PeerOne_KnowledgeGraph", files)
    m.cls("ClientA_KnowledgeGraph", [f"other/{i}.md" for i in range(3)])
    scan = m.scan()
    (verdict,) = scan.verdicts
    assert verdict.bound == "PeerOne_KnowledgeGraph"
    assert verdict.project_name == PEER
    assert not verdict.mismatch
    # The basename-shaped class is NOT read as this project's.
    assert [u.name for u in scan.unclaimed] == ["ClientA_KnowledgeGraph"]


def test_drift_census_scans_the_bound_collection_not_a_basename_one(tmp_path, monkeypatch):
    from vco_lib import doctor, kg_sync_drift

    db = make_launcher_db(tmp_path)
    add_project(db, project_id="p-peer", name=PEER, folder_path=tmp_path / "client-a-checkout",
                kg_primary="PeerOne_KnowledgeGraph")
    monkeypatch.setenv("VCT_LAUNCHER_DB_PATH", str(db))
    seen: list[str] = []

    def fake_scan(knowledge_dir, *, weaviate_url, kg_collection, shared_kg_collection):
        seen.append(kg_collection)
        return SimpleNamespace(status="ok", stored_only=(), missing=(), stale=(), drifted=(), detail="")

    monkeypatch.setattr(kg_sync_drift, "scan_drift", fake_scan)
    findings = doctor.probe_kg_drift_census(tmp_path, doctor.DoctorResolvers(), {})
    assert seen == ["PeerOne_KnowledgeGraph"]
    assert findings[0].status == doctor.STATUS_OK


def test_infra_declarations_probe_names_declarers_by_binding(tmp_path):
    from vco_lib import doctor

    db = make_launcher_db(tmp_path)
    add_project(db, project_id="p-peer", name=PEER, folder_path=tmp_path / "client-a-checkout")
    _declare(db, "p-peer", infra.KIND_UNMANAGED_CONTAINER, "peer-weaviate")
    _declare(db, "p-peer", infra.KIND_LOCAL_PORT, "11436")
    res = doctor.DoctorResolvers(infra_declarations=lambda: infra.load(db_path=db, env={}))
    (f,) = doctor.probe_infra_declarations(tmp_path, res, {})
    assert f.status == doctor.STATUS_OK
    assert f"peer-weaviate (project '{PEER}')" in f.summary
    assert "client-a-checkout" not in f.summary
    assert doctor.probe_infra_declarations(
        tmp_path, doctor.DoctorResolvers(infra_declarations=lambda: infra.Declarations(True)), {}) == []
    (u,) = doctor.probe_infra_declarations(
        tmp_path, doctor.DoctorResolvers(infra_declarations=lambda: infra.Declarations(False)), {})
    assert u.status == doctor.STATUS_UNKNOWN


def test_codegraph_references_derives_no_project_identity():
    """codegraph_references resolves CODE references (module stems, class
    full names); it never names a project. Pinned so a project-identity
    derivation from a folder name cannot creep in unseen."""
    src = (REPO_ROOT / "vco_lib/codegraph_references.py").read_text(encoding="utf-8")
    assert "project_identity" not in src
    for token in ("folder_path", "PROJECT_NAME", "os.path.basename", "Path.cwd"):
        assert token not in src, token


# ---------------------------------------------------------------------------
# M3-4: unmanaged containers — service detection (adoption) and compose recovery
# ---------------------------------------------------------------------------

def _container(name: str, *, image: str, port: int, running: bool, runtime: str):
    from vco_lib import service_detection as det

    # docker labels compose projects with com.docker.compose.*, podman-compose
    # with io.podman.compose.*; the inspect parse reads both.
    family = "com.docker.compose" if runtime == "docker" else "io.podman.compose"
    obj = {
        "Name": "/" + name if runtime == "docker" else name,
        "Config": {"Image": image, "Labels": {f"{family}.project": "peer", f"{family}.service": "weaviate"},
                   "Env": []},
        "State": {"Running": running, "Status": "running" if running else "exited"},
        "HostConfig": {"PortBindings": {"8080/tcp": [{"HostIp": "", "HostPort": str(port)}]}},
        "Mounts": [],
    }
    info = det.parse_inspect(obj)
    assert info is not None and info.name == name, obj
    return info


@pytest.mark.parametrize("runtime", ["docker", "podman"])
def test_a_declared_container_is_never_an_adoption_candidate(runtime):
    from vco_lib import service_detection as det

    peer = _container("peer-weaviate", image="semitechnologies/weaviate:1.30", port=8082,
                      running=False, runtime=runtime)
    common = dict(runtime=runtime, installer_project="infrastructure", containers=[peer],
                  probe_defaults=False, fetch=lambda *a, **k: None, tcp_open=lambda *a: False,
                  declared_ports=())
    undeclared = det.detect(unmanaged=(), **common)
    assert [c.container.name for c in undeclared.for_service("weaviate")] == ["peer-weaviate"]
    declared = det.detect(unmanaged=("peer-weaviate",), **common)
    assert declared.for_service("weaviate") == []
    assert 8082 in declared.reserved_ports
    assert [c.name for c in declared.containers] == ["peer-weaviate"], "the listing itself is kept"


def test_a_declared_local_port_is_never_probed():
    from vco_lib import service_detection as det

    probed: list[str] = []

    def fetch(url, timeout, **kw):
        probed.append(url)
        return None

    det.detect(runtime=None, containers=[], fetch=fetch, tcp_open=lambda *a: False,
               services=("ollama",), unmanaged=(), declared_ports=(11435,))
    assert not any(":11435" in u for u in probed), probed
    assert any(":11434" in u for u in probed), "undeclared default ports are still probed"


def _storage_failure():
    from vco_lib import compose_recovery as cr

    return cr.ComposeFailure(cause=cr.NAME_CONFLICT_STORAGE_LEFTOVER, evidence="name in use", remedy="",
                             healable=True, subject="vco_ollama")


def test_storage_heal_refuses_a_declared_container_and_an_unreadable_list():
    from vco_lib import compose_recovery as cr

    calls: list[list[str]] = []

    def run(argv, **kw):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 1, "", "unreachable in this test")

    res = cr.heal(_storage_failure(), runtime="podman", run=run, log=lambda m: None,
                  unmanaged={"vco_ollama": f"declared unmanaged by project '{PEER}'"})
    assert not res.healed and PEER in res.reason and calls == []
    # Undeclared: the guard lets it through to the next (read-only) check.
    res = cr.heal(_storage_failure(), runtime="podman", run=run, log=lambda m: None, unmanaged={})
    assert calls and calls[0][:3] == ["podman", "ps", "-a"]
    assert "could not list containers" in res.reason


def test_storage_heal_refuses_when_the_declarations_cannot_be_read(tmp_path, monkeypatch):
    from vco_lib import compose_recovery as cr

    monkeypatch.setattr(infra, "load", lambda **kw: infra.Declarations(resolvable=False))
    res = cr.heal(_storage_failure(), runtime="podman", run=lambda *a, **k: pytest.fail("no runtime call"),
                  log=lambda m: None)
    assert not res.healed and "could not be read" in res.reason


def test_storage_heal_on_docker_is_not_applicable_either_way():
    from vco_lib import compose_recovery as cr

    for unmanaged in ({}, {"vco_ollama": "declared"}):
        res = cr.heal(_storage_failure(), runtime="docker", run=lambda *a, **k: pytest.fail("no call"),
                      log=lambda m: None, unmanaged=unmanaged)
        assert not res.healed


def test_code_embed_rebuild_is_not_printed_for_a_declared_container(tmp_path):
    from vco_lib import code_embed_image, doctor

    (tmp_path / "vco_lib").mkdir()
    (tmp_path / "vco_lib" / "__init__.py").write_text("", encoding="utf-8")
    stale = code_embed_image.ImageState(code_embed_image.STALE, "code_embed: image is stale.")
    declared = infra.Declarations(True, (infra.Declaration(infra.KIND_UNMANAGED_CONTAINER, "vco_code_embed",
                                                          f"project '{PEER}'"),))
    res = doctor.DoctorResolvers(code_embed_state=lambda root: stale, infra_declarations=lambda: declared,
                                 code_embed_rebuild_context=lambda: code_embed_image.RebuildContext())
    (f,) = doctor.probe_code_embed_image(tmp_path, res, {})
    assert f.status == doctor.STATUS_OK and not f.command and PEER in f.summary
    res.infra_declarations = lambda: infra.Declarations(True)
    (f,) = doctor.probe_code_embed_image(tmp_path, res, {})
    assert f.status == doctor.STATUS_PROBLEM and f.condition_id == doctor.CID_CODE_EMBED_IMAGE_STALE


# ---------------------------------------------------------------------------
# P299-B2: the compose override pair
# ---------------------------------------------------------------------------

_MARKER = "# Auto-generated by VCT (test)\n"


def _pair(tmp: Path, canonical: str | None, legacy: str | None) -> Path:
    infra_dir = tmp / "infrastructure"
    infra_dir.mkdir(parents=True, exist_ok=True)
    if canonical is not None:
        (infra_dir / cop.CANONICAL_NAME).write_text(canonical, encoding="utf-8")
    if legacy is not None:
        (infra_dir / cop.LEGACY_NAME).write_text(legacy, encoding="utf-8")
    return infra_dir


def test_pair_state_arms(tmp_path):
    a = _MARKER + "services:\n  ollama:\n    restart: always\n"
    b = _MARKER + "services:\n  ollama:\n    restart: \"no\"\n"
    assert cop.pair_state(_pair(tmp_path / "1", None, None)) == cop.PAIR_ABSENT
    assert cop.pair_state(_pair(tmp_path / "2", a, None)) == cop.PAIR_SINGLE
    assert cop.pair_state(_pair(tmp_path / "3", a, a)) == cop.PAIR_EQUAL
    assert cop.pair_state(_pair(tmp_path / "4", a, a + "# trailing comment\n")) == cop.PAIR_EQUAL
    assert cop.pair_state(_pair(tmp_path / "5", a, b)) == cop.PAIR_DIVERGENT


def test_the_adoption_writer_refuses_a_divergent_pair_and_writes_an_equal_one(tmp_path):
    from vco_lib import service_adoption as sa

    a = _MARKER + "services:\n  ollama:\n    restart: always\n"
    b = _MARKER + "services:\n  ollama:\n    restart: \"no\"\n"
    infra_dir = _pair(tmp_path / "div", a, b)
    refusal = sa.write_adoption_override(infra_dir, _MARKER + "services: {}\n")
    assert refusal and "DIFFERENT configurations" in refusal
    assert (infra_dir / cop.CANONICAL_NAME).read_text() == a
    assert (infra_dir / cop.LEGACY_NAME).read_text() == b, "neither half overwritten"
    infra_dir = _pair(tmp_path / "eq", a, a)
    assert sa.write_adoption_override(infra_dir, _MARKER + "services: {}\n") is None
    assert (infra_dir / cop.LEGACY_NAME).read_text() == _MARKER + "services: {}\n"


def _install_root(tmp: Path) -> Path:
    (tmp / "vco_lib").mkdir(parents=True, exist_ok=True)
    (tmp / "vco_lib" / "__init__.py").write_text("", encoding="utf-8")
    return tmp


def test_doctor_reports_a_divergent_pair_and_the_same_reading_clears_it(tmp_path):
    from vco_lib import deferral_probes, doctor

    root = _install_root(tmp_path)
    _pair(root, _MARKER + "a: 1\n", _MARKER + "a: 2\n")
    (f,) = doctor.probe_compose_override_pair(root, doctor.DoctorResolvers(), {})
    assert f.status == doctor.STATUS_PROBLEM
    assert f.condition_id == doctor.CID_COMPOSE_OVERRIDE_PAIR_DIVERGED
    assert cop.LEGACY_NAME in f.command and cop.CANONICAL_NAME in f.command
    (entry,) = doctor.deferral_entries_for(doctor.DoctorReport(folder=root, findings=[f], scope=doctor.SCOPE_FULL))
    assert entry.condition_id == "compose_override_pair_diverged"
    ctx = deferral_probes.ProbeContext(folder=root)
    assert deferral_probes.PROBES["compose_override_pair_still_diverged"](ctx) is True
    # Leave-alone arm: make them agree → OK finding carrying the cid → resolved.
    (root / "infrastructure" / cop.LEGACY_NAME).write_text(_MARKER + "a: 1\n", encoding="utf-8")
    (f,) = doctor.probe_compose_override_pair(root, doctor.DoctorResolvers(), {})
    assert f.status == doctor.STATUS_OK
    assert doctor.healthy_condition_ids(doctor.DoctorReport(folder=root, findings=[f], scope=doctor.SCOPE_FULL)) == \
        ["compose_override_pair_diverged"]
    assert deferral_probes.PROBES["compose_override_pair_still_diverged"](ctx) is False
    assert deferral_probes.PROBES["compose_override_pair_still_diverged"](
        deferral_probes.ProbeContext(folder=tmp_path / "not-a-root")) is None
    assert doctor.probe_compose_override_pair(tmp_path / "not-a-root", doctor.DoctorResolvers(), {}) == []


def test_new_conditions_are_registered_with_class_and_clear_probe():
    import tomllib

    reg = tomllib.loads((REPO_ROOT / "vco_lib/deferral_conditions.toml").read_text(encoding="utf-8"))
    pair = reg["conditions"]["compose_override_pair_diverged"]
    assert pair["class"] == "action_required"
    assert pair["clear_probe"] == "probe:py:compose_override_pair_still_diverged"
    gave_up = reg["conditions"]["watchdog_gave_up"]
    assert gave_up["class"] == "action_required" and gave_up["owner"].startswith("vct-hub")
    assert gave_up["clear_probe"] == "paired-resolution"
    # The Rust emitter names the same id.
    rs = (REPO_ROOT / "launcher/src-tauri/vct-hub/src/infra_watchdog.rs").read_text(encoding="utf-8")
    assert 'condition_id: "watchdog_gave_up"' in rs


# ---------------------------------------------------------------------------
# The three resolvers read the new /config keys alike
# ---------------------------------------------------------------------------

def test_python_resolver_parses_the_declaration_lists():
    from vco_lib import project_config as pc

    assert pc.INFRA_LIST_FIELDS == ("external_collections", "unmanaged_containers", "ssrf_extra_local_ports")
    from tests.test_project_config import FULL_BODY

    cfg = pc._from_hub_body({**FULL_BODY, "external_collections": ["PeerRuntime_Memory"],
                             "unmanaged_containers": ["peer-weaviate"], "ssrf_extra_local_ports": [11436, "x", 0]})
    assert cfg.external_collections == ("PeerRuntime_Memory",)
    assert cfg.unmanaged_containers == ("peer-weaviate",)
    assert cfg.ssrf_extra_local_ports == (11436,)
    old = pc._from_hub_body(dict(FULL_BODY))
    assert old.external_collections == () and old.ssrf_extra_local_ports == ()


class _Stub(http.server.BaseHTTPRequestHandler):
    body: dict[str, Any] = {}

    def do_GET(self):  # noqa: N802
        payload = json.dumps(self.__class__.body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *a, **k):
        pass


@pytest.fixture
def stub_hub():
    _Stub.body = {"external_collections": ["PeerRuntime_Memory", "PeerRuntime_Dialogue"],
                  "unmanaged_containers": [], "ssrf_extra_local_ports": [11436]}
    server = http.server.HTTPServer(("127.0.0.1", 0), _Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    deadline = time.time() + 2
    while time.time() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.05)
    yield port
    server.shutdown()


_EXPECTED = {
    "external_collections": "PeerRuntime_Memory\nPeerRuntime_Dialogue\n",
    "unmanaged_containers": "",
    "ssrf_extra_local_ports": "11436\n",
}


def _resolver_env(port: int) -> dict[str, str]:
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": tempfile.mkdtemp(),
            "VCT_HUB_PORT": str(port), "VCT_HUB_TOKEN": "t",
            "VCT_STATE_DIR": tempfile.mkdtemp(prefix="w5a-resolver-")}


@pytest.mark.skipif(sys.platform == "win32", reason="bash sibling")
@pytest.mark.parametrize("field", list(_EXPECTED))
def test_bash_resolver_renders_one_value_per_line(stub_hub, field):
    out = subprocess.run(["bash", str(REPO_ROOT / "templates/scripts/vct_project_config.sh"), "dummy-id",
                          "--field", field], env=_resolver_env(stub_hub), capture_output=True, text=True,
                         timeout=30)
    assert out.returncode == 0, out.stderr
    assert out.stdout == _EXPECTED[field]


_PWSH = shutil.which("pwsh")


@pytest.mark.skipif(_PWSH is None, reason="pwsh not installed")
@pytest.mark.parametrize("field", list(_EXPECTED))
def test_ps1_resolver_renders_one_value_per_line(stub_hub, field):
    from tests.common.pwsh import PWSH_BASE_FLAGS

    out = subprocess.run([_PWSH, *PWSH_BASE_FLAGS, "-NonInteractive", "-File",
                          str(REPO_ROOT / "templates/scripts/vct_project_config.ps1"),
                          "-Project", "dummy-id", "-Field", field],
                         env=_resolver_env(stub_hub), capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.replace("\r\n", "\n") == _EXPECTED[field]


# ---------------------------------------------------------------------------
# Follow-up 2026-10-09: ONE condition for a divergent pair — the install-time
# rename pass emits/clears compose_override_pair_diverged; the retired
# compose_override_filename_conflict drains.
# ---------------------------------------------------------------------------

def _pair_in(d: Path, canonical: str, legacy: str) -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / cop.CANONICAL_NAME).write_text(canonical, encoding="utf-8")
    (d / cop.LEGACY_NAME).write_text(legacy, encoding="utf-8")


def _ledger_ids(root: Path) -> list[str]:
    from vco_lib.deferral_report import DeferralReport

    return [e.condition_id for e in DeferralReport.read(root).entries]


def _seed_old_conflict_entry(root: Path) -> None:
    from vco_lib.deferral_emit import emit_entries
    from vco_lib.deferral_report import DeferralEntry

    emit_entries(root, [DeferralEntry(
        condition_id="compose_override_filename_conflict",
        title="Both legacy and canonical compose override files present",
        detected="pre-v0.2.102 entry", why_deferred="x", command_to_apply="x",
    )])


@pytest.mark.parametrize("subdir", list(cop.SEARCH_SUBDIRS))
def test_install_pass_writes_the_new_id_and_the_doctor_reads_the_same_state(tmp_path, subdir):
    from vco_lib import deferral_probes, doctor, project_init

    root = _install_root(tmp_path)
    _pair_in(root / subdir, "user: edited\n", "user: original\n")  # hand-written, divergent
    res = project_init._detect_and_rename_legacy_compose_override(root)
    assert res is not None and res["conflicts"]
    ids = _ledger_ids(root)
    assert "compose_override_pair_diverged" in ids
    assert "compose_override_filename_conflict" not in ids, "the retired id is never written"
    from vco_lib.deferral_report import DeferralReport

    entry = [e for e in DeferralReport.read(root).entries
             if e.condition_id == "compose_override_pair_diverged"][0]
    assert subdir in entry.command_to_apply
    # The doctor and the clear probe read the same state, in the same subdir.
    (f,) = doctor.probe_compose_override_pair(root, doctor.DoctorResolvers(), {})
    assert f.status == doctor.STATUS_PROBLEM and subdir in f.summary
    assert deferral_probes.PROBES["compose_override_pair_still_diverged"](
        deferral_probes.ProbeContext(folder=root)) is True
    # Leave-alone arm: the halves agree → the next pass clears the entry.
    (root / subdir / cop.LEGACY_NAME).write_text("user: edited\n", encoding="utf-8")
    res = project_init._detect_and_rename_legacy_compose_override(root)
    assert "compose_override_pair_diverged" not in _ledger_ids(root)
    assert "compose_override_pair_diverged" in res["auto_resolved_condition_ids"]


def test_a_retired_old_id_entry_drains_on_the_next_pass_and_is_replaced_when_still_true(tmp_path):
    from vco_lib import project_init

    # Arm 1: the pair has since converged → the old entry clears, nothing new.
    root = _install_root(tmp_path / "converged")
    _pair_in(root / "infrastructure", "a: 1\n", "a: 1\n")
    _seed_old_conflict_entry(root)
    assert "compose_override_filename_conflict" in _ledger_ids(root)
    res = project_init._detect_and_rename_legacy_compose_override(root)
    assert _ledger_ids(root) == []
    assert "compose_override_filename_conflict" in res["auto_resolved_condition_ids"]
    # Arm 2: still divergent → the old id drains and the NEW id carries the state.
    root = _install_root(tmp_path / "still")
    _pair_in(root / "infrastructure", "a: 1\n", "a: 2\n")
    _seed_old_conflict_entry(root)
    project_init._detect_and_rename_legacy_compose_override(root)
    ids = _ledger_ids(root)
    assert "compose_override_filename_conflict" not in ids
    assert "compose_override_pair_diverged" in ids


def test_the_old_id_is_registered_retired_and_names_its_replacement():
    import tomllib

    reg = tomllib.loads((REPO_ROOT / "vco_lib/deferral_conditions.toml").read_text(encoding="utf-8"))
    old = reg["conditions"]["compose_override_filename_conflict"]
    assert old["status"] == "retired"
    assert "compose_override_pair_diverged" in old["notes"]
    # No producer emits the retired id any more.
    for rel in ("vco_lib/project_init.py", "vco_lib/doctor.py", "vco_lib/doctor_infra.py",
                "vco_lib/compose_override_pair.py"):
        src = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert 'condition_id="compose_override_filename_conflict"' not in src, rel


def test_both_emitters_build_the_same_entry(tmp_path):
    from vco_lib import doctor, doctor_infra

    root = _install_root(tmp_path)
    _pair_in(root / "infrastructure", "a: 1\n", "a: 2\n")
    (f,) = doctor.probe_compose_override_pair(root, doctor.DoctorResolvers(), {})
    from_doctor = doctor_infra.compose_override_pair_entry(f)
    from_install = cop.divergent_pairs_entry(cop.divergent_pairs(root))
    assert from_doctor.title == from_install.title
    assert from_doctor.why_deferred == from_install.why_deferred
    assert from_doctor.command_to_apply == from_install.command_to_apply


# ---------------------------------------------------------------------------
# Review A1: the adoption feed refuses when the declarations are unreadable
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("runtime", ["docker", "podman"])
def test_detection_offers_nothing_when_the_declarations_cannot_be_read(runtime, monkeypatch):
    from vco_lib import service_detection as det

    peer = _container("peer-weaviate", image="semitechnologies/weaviate:1.30", port=8082,
                      running=False, runtime=runtime)
    probed: list[str] = []

    def fetch(url, timeout, **kw):
        probed.append(url)
        return None

    common = dict(runtime=runtime, installer_project="infrastructure", containers=[peer],
                  fetch=fetch, tcp_open=lambda *a: False)
    # Arm 1 — resolvable, nothing declared: offered exactly as before.
    monkeypatch.setattr(infra, "load", lambda **kw: infra.Declarations(resolvable=True))
    offered = det.detect(**common)
    assert [c.container.name for c in offered.for_service("weaviate") if c.container] == ["peer-weaviate"]
    assert offered.refused is None
    # Arm 2 — a launcher.db that exists but cannot be read: nothing offered,
    # nothing probed, every listed port reserved, and the reason is the one
    # every other consumer refuses with.
    probed.clear()
    monkeypatch.setattr(infra, "load", lambda **kw: infra.Declarations(resolvable=False))
    refused = det.detect(**common)
    assert all(refused.for_service(s) == [] for s in det.SERVICES)
    assert probed == []
    assert refused.refused == infra.UNREADABLE_REASON
    assert 8082 in refused.reserved_ports
    assert [c.name for c in refused.containers] == ["peer-weaviate"], "the listing itself is kept"


def test_detection_refusal_reads_a_real_unreadable_launcher_db(tmp_path, monkeypatch):
    from vco_lib import service_detection as det

    bad = tmp_path / "launcher.db"
    bad.write_bytes(b"this is not sqlite" * 100)
    monkeypatch.setenv("VCT_LAUNCHER_DB_PATH", str(bad))
    out = det.detect(runtime=None, containers=[], fetch=lambda *a, **k: pytest.fail("no probe"),
                     tcp_open=lambda *a: False)
    assert out.refused == infra.UNREADABLE_REASON
