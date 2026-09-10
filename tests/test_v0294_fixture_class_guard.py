# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.94 W-WEAVIATE: the fixture-shaped class write guard + suite containment.

The incident: the maintainer's live Weaviate held ``Alpha_KnowledgeGraph`` with
70 REAL ``knowledge/concepts/*.md`` nodes. ``Alpha`` is not a project — it is
this suite's fixture project name. Two independent legs close it, and each is
tested to hold ALONE:

  * the WRITE GUARD (``vco_lib.fixture_class_guard``), enforced at the three
    entry points that can mint or fill such a class — the MCP's
    ``store_knowledge_node``, ``sync_knowledge_graph.main``, and the diagram
    indexer's one write seam;
  * the SUITE PIN (``tests/conftest.py``), which points ``WEAVIATE_URL`` at an
    unroutable sentinel by default so a plain ``pytest`` cannot reach a live
    backend at all.

The enforcement tests DRIVE the real call sites with a fake client and assert
the client is never touched — not "the source mentions the guard". A name in a
comment satisfies a source scan; only a mutated call site proves the wiring.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import os
import re
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests import conftest as _conftest  # noqa: E402
from vco_lib import fixture_class_guard as fcg  # noqa: E402

TESTS_DIR = REPO_ROOT / "tests"


# ---------------------------------------------------------------------------
# 1. The table is GROUNDED and SAFE
# ---------------------------------------------------------------------------


def _class_names_used_by_tests() -> set[str]:
    """Every ``<Stem>_<Family>`` literal that appears anywhere under tests/.

    Derived from the tree, not hand-listed: a fixture name that stops being
    used must stop being a reason to refuse a real user's write.
    """
    families = "|".join(re.escape(f) for f in fcg.COLLECTION_FAMILY_SUFFIXES)
    pattern = re.compile(rf"\b([A-Za-z0-9_]+)_({families})\b")
    found: set[str] = set()
    for path in TESTS_DIR.rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="replace")
        for stem, family in pattern.findall(text):
            found.add(f"{stem}_{family}")
    return found


def test_every_table_entry_is_grounded_in_the_test_corpus():
    """No invented entries: each stem must really be a fixture name here.

    The table's cost is borne by any user whose project happens to carry one of
    these names, so an entry that no test uses is pure downside.
    """
    used = {n.casefold() for n in _class_names_used_by_tests()}
    for stem in sorted(fcg.FIXTURE_PROJECT_NAMES):
        assert any(
            n.startswith(stem.casefold() + "_") for n in used
        ), (
            f"'{stem}' is in FIXTURE_PROJECT_NAMES but no test under "
            f"{TESTS_DIR} names a '{stem}_<family>' class. Either a test was "
            f"deleted (drop the entry) or the entry was invented."
        )


def test_the_incident_stems_are_covered():
    """Alpha (70 live rows) plus the Beta/Gamma peers it fans out to."""
    for stem in ("Alpha", "Beta", "Gamma"):
        assert stem in fcg.FIXTURE_PROJECT_NAMES


def test_no_real_shipped_project_name_is_in_the_table():
    """A shipped default in the table would refuse real users' writes.

    ``tests/`` mentions ~200 distinct ``<Stem>_KnowledgeGraph`` literals, and
    the set includes the REAL names (the canonical shared collection, the
    maintainer's own project) plus generic stems a real folder plausibly
    carries. That is why the table is curated rather than derived — and this
    test is the floor under the curation.
    """
    from vco_lib.kg_binding_doctor import DEFAULT_SHARED_KG_COLLECTION

    real_stem = DEFAULT_SHARED_KG_COLLECTION.rsplit("_", 1)[0]
    folded = {n.casefold() for n in fcg.FIXTURE_PROJECT_NAMES}
    forbidden = {
        real_stem,
        "VCODev",
        "VibeCodedTools",
        # Generic stems a user's folder plausibly IS. Named here so a future
        # "let's just derive the whole set from tests/" cannot land quietly.
        "Demo", "Test", "TestProject", "Project", "Shared", "My", "MyProject",
        "New", "Old", "Other", "Legacy", "Sample", "Widget",
    }
    collisions = sorted(s for s in forbidden if s.casefold() in folded)
    assert not collisions, (
        f"FIXTURE_PROJECT_NAMES must never hold a name a real project could "
        f"own — found {collisions}. Refusing those would break real installs."
    )


def test_families_reuse_the_shipped_code_suffix_list():
    """The five code families come from weaviate_schema, not a third copy."""
    from vco_lib.weaviate_schema import _CODE_COLLECTION_SUFFIXES

    assert set(_CODE_COLLECTION_SUFFIXES) <= set(fcg.COLLECTION_FAMILY_SUFFIXES)
    assert {"KnowledgeGraph", "Development", "Diagrams"} <= set(
        fcg.COLLECTION_FAMILY_SUFFIXES
    )


# ---------------------------------------------------------------------------
# 2. The predicate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "class_name,stem",
    [
        ("Alpha_KnowledgeGraph", "Alpha"),
        ("Beta_Development", "Beta"),
        ("Gamma_Diagrams", "Gamma"),
        ("Foo_CodeFunction", "Foo"),
        ("Bar_CodeInteraction", "Bar"),
        # Weaviate capitalises the first letter on POST, so both spellings of
        # the same class must resolve to the same table entry.
        ("foo_KnowledgeGraph", "Foo"),
        ("ALPHA_KNOWLEDGEGRAPH", "Alpha"),
    ],
)
def test_fixture_shaped_positives(class_name, stem):
    assert fcg.fixture_stem_of(class_name) == stem
    assert fcg.is_fixture_shaped_class(class_name)


@pytest.mark.parametrize(
    "class_name",
    [
        "VCODev_KnowledgeGraph",
        "VibeCodedOrchestrator_KnowledgeGraph",
        # Underscored real prefixes: a split-on-first-underscore rule would
        # read this stem as "VCT" and this one as "Orchestrator".
        "VCT_transcrypt_CodeAPI",
        "Orchestrator_root_CodeFunction",
        # Substring, not stem.
        "Alphabet_KnowledgeGraph",
        "AlphaBeta_KnowledgeGraph",
        "MyAlpha_KnowledgeGraph",
        # No family suffix at all.
        "Alpha",
        "Alpha_Something",
        "",
        None,
        123,
    ],
)
def test_fixture_shaped_negatives(class_name):
    assert fcg.fixture_stem_of(class_name) is None
    assert not fcg.is_fixture_shaped_class(class_name)


# ---------------------------------------------------------------------------
# 3. The guard: refuse LOUDLY unless the process declares ownership
# ---------------------------------------------------------------------------


def test_guard_refuses_an_undeclared_fixture_write(monkeypatch):
    monkeypatch.delenv(fcg.ALLOW_FIXTURE_WRITES_ENV, raising=False)
    with pytest.raises(fcg.FixtureClassWriteRefused) as excinfo:
        fcg.guard_fixture_class_write(
            "Alpha_KnowledgeGraph",
            operation="store a knowledge node in",
            weaviate_url="http://localhost:8081",
        )
    message = str(excinfo.value)
    # A refusal nobody can act on is a silent skip with extra steps: the
    # message must name the class, the reason, and the declaration.
    assert "Alpha_KnowledgeGraph" in message
    assert "Alpha" in message
    assert "http://localhost:8081" in message
    assert fcg.ALLOW_FIXTURE_WRITES_ENV in message
    assert excinfo.value.stem == "Alpha"
    assert excinfo.value.class_name == "Alpha_KnowledgeGraph"


@pytest.mark.parametrize("declared", ["1", "true", "TRUE", "yes"])
def test_guard_allows_a_declared_fixture_write(monkeypatch, declared):
    monkeypatch.setenv(fcg.ALLOW_FIXTURE_WRITES_ENV, declared)
    fcg.guard_fixture_class_write("Alpha_KnowledgeGraph")  # must not raise


@pytest.mark.parametrize("undeclared", ["", "0", "false", "no"])
def test_falsy_spellings_do_not_count_as_a_declaration(monkeypatch, undeclared):
    monkeypatch.setenv(fcg.ALLOW_FIXTURE_WRITES_ENV, undeclared)
    with pytest.raises(fcg.FixtureClassWriteRefused):
        fcg.guard_fixture_class_write("Alpha_KnowledgeGraph")


def test_guard_never_touches_a_real_project_class(monkeypatch):
    monkeypatch.delenv(fcg.ALLOW_FIXTURE_WRITES_ENV, raising=False)
    for name in ("VCODev_KnowledgeGraph", "VCT_transcrypt_CodeAPI", "Nope"):
        fcg.guard_fixture_class_write(name)  # must not raise


def test_the_declaration_is_read_at_call_time(monkeypatch):
    """A harness that sets the variable mid-run is honoured (import-time
    caching would make the escape hatch unusable from a running process)."""
    monkeypatch.delenv(fcg.ALLOW_FIXTURE_WRITES_ENV, raising=False)
    assert fcg.fixture_writes_allowed() is False
    monkeypatch.setenv(fcg.ALLOW_FIXTURE_WRITES_ENV, "1")
    assert fcg.fixture_writes_allowed() is True


# ---------------------------------------------------------------------------
# 4. The suite pin (tests/conftest.py) — both branches of the decision
# ---------------------------------------------------------------------------


def test_this_process_is_pinned_at_the_unroutable_sentinel():
    """Wiring, not intent: THIS test is a normal (non-opt-out) file."""
    assert os.environ["WEAVIATE_URL"] == fcg.UNROUTABLE_SENTINEL_URL


def test_the_suite_declares_fixture_class_ownership():
    """Otherwise the guard would refuse the suite's own fixture writes."""
    assert os.environ[fcg.ALLOW_FIXTURE_WRITES_ENV] == "1"


def test_default_decision_is_the_sentinel():
    assert (
        _conftest._weaviate_url_pin_for("test_anything_at_all.py")
        == fcg.UNROUTABLE_SENTINEL_URL
    )


def test_opt_out_decision_restores_the_ambient_value():
    for name in sorted(_conftest._LIVE_WEAVIATE_OPT_OUT_FILES):
        assert (
            _conftest._weaviate_url_pin_for(name)
            == _conftest._AMBIENT_WEAVIATE_URL
        ), f"{name} must see the ambient WEAVIATE_URL, not the pin"


def test_the_preship_live_gate_file_is_in_the_opt_out_list():
    """Derived from the gate script, so the two cannot drift apart.

    If the pin covered this file it would SKIP, pytest would exit 0, and
    ``scripts/pre-ship-check.sh`` would print PASS for a run that asserted
    nothing — a green gate measuring nothing is worse than a red one.
    """
    gate = (REPO_ROOT / "scripts" / "pre-ship-check.sh").read_text(
        encoding="utf-8"
    )
    match = re.search(r'LIVE_GATE_TEST="tests/([A-Za-z0-9_.]+\.py)"', gate)
    assert match, "pre-ship-check.sh no longer names a LIVE_GATE_TEST"
    assert match.group(1) in _conftest._LIVE_WEAVIATE_OPT_OUT_FILES


def test_the_opt_out_list_names_only_files_that_exist():
    for name in sorted(_conftest._LIVE_WEAVIATE_OPT_OUT_FILES):
        assert (TESTS_DIR / name).is_file(), (
            f"{name} is in the live-Weaviate opt-out list but does not exist; "
            f"a stale entry silently un-pins nothing and hides a deleted gate."
        )


# ---------------------------------------------------------------------------
# 5. Enforcement — the real call sites, driven
# ---------------------------------------------------------------------------


class _RecordingCollections:
    def __init__(self) -> None:
        self.requested: list[str] = []
        self.created: list[str] = []

    def get(self, name):  # pragma: no cover - must never be reached
        self.requested.append(name)
        raise AssertionError(f"guard let a write through to {name!r}")

    def create(self, name=None, **kwargs):
        self.created.append(name)
        return True

    def exists(self, name):
        return False


class _RecordingClient:
    def __init__(self) -> None:
        self.collections = _RecordingCollections()


def _weaviate_mcp_modules() -> list[str]:
    return [
        name
        for name in list(sys.modules)
        if name == "weaviate_mcp" or name.startswith("weaviate_mcp.")
    ]


@pytest.fixture
def mcp_server_module():
    """``weaviate_mcp.server`` FROM THIS CHECKOUT, not from the dev venv's pin.

    This machine's venv carries an editable ``.pth`` that puts the
    MAINTAINER's clone (``…/VCO_dev/claude_mcp_servers``) on ``sys.path``, so a
    bare ``import weaviate_mcp.server`` from a worktree loads ANOTHER tree's
    copy — the in-process twin of the shadow ``tests/common/child_env.py``
    documents for child processes. A guard test that measured that copy would
    pass or fail for reasons unrelated to the change under test, so pin the
    path, purge the cache, assert what we got, and restore both.
    """
    local = str(REPO_ROOT / "claude_mcp_servers")
    saved = {name: sys.modules[name] for name in _weaviate_mcp_modules()}
    for name in saved:
        del sys.modules[name]
    sys.path.insert(0, local)
    try:
        mod = importlib.import_module("weaviate_mcp.server")
        assert Path(mod.__file__).is_relative_to(REPO_ROOT), (
            f"loaded {mod.__file__} instead of this checkout's copy — the "
            f"test would be measuring a different tree"
        )
        yield mod
    finally:
        try:
            sys.path.remove(local)
        except ValueError:  # pragma: no cover - defensive
            pass
        for name in _weaviate_mcp_modules():
            del sys.modules[name]
        sys.modules.update(saved)


def _unwrap(tool):
    return getattr(tool, "fn", None) or getattr(tool, "__wrapped__", None) or tool


def test_store_knowledge_node_refuses_a_fixture_target(
    monkeypatch, tmp_path, mcp_server_module
):
    """The MCP write path: an error PAYLOAD, no file, no collection handle."""
    srv = mcp_server_module
    client = _RecordingClient()
    monkeypatch.setattr(srv, "get_weaviate_client", lambda: client)
    monkeypatch.setattr(srv, "_assert_workspace_unchanged", lambda *a, **k: None)
    monkeypatch.setattr(srv, "KG_COLLECTION", "Alpha_KnowledgeGraph")
    monkeypatch.setattr(srv, "KG_BASE_DIR", str(tmp_path))
    monkeypatch.delenv(fcg.ALLOW_FIXTURE_WRITES_ENV, raising=False)
    monkeypatch.delenv("VCT_PROJECT_ID", raising=False)

    fn = _unwrap(srv.store_knowledge_node)
    result = json.loads(
        asyncio.run(
            fn(
                title="Ghost",
                content="body",
                node_type="concept",
                tags=[],
                links=[],
                file_path="knowledge/concepts/ghost.md",
                scope="project",
            )
        )
    )

    assert result["status"] == "error"
    assert result["target_collection"] == "Alpha_KnowledgeGraph"
    assert result["fixture_project_name"] == "Alpha"
    assert result["file_written"] is False
    assert fcg.ALLOW_FIXTURE_WRITES_ENV in result["error"]
    # Nothing reached Weaviate, and no stray .md landed on disk.
    assert client.collections.requested == []
    assert list(tmp_path.rglob("*.md")) == []


def _load_sync_module() -> types.ModuleType:
    os.environ.setdefault("VCT_ORCHESTRATOR_ROOT", str(REPO_ROOT))
    path = REPO_ROOT / "templates" / "scripts" / "sync_knowledge_graph.py"
    spec = importlib.util.spec_from_file_location(
        "_v0294_fixture_guard_sync_kg", path
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def sync_mod() -> types.ModuleType:
    return _load_sync_module()


def test_sync_refuses_a_fixture_kg_target(monkeypatch, sync_mod):
    """``sync_knowledge_graph`` exits 2 BEFORE any backend connection.

    This is the writer that produced the incident's 70 rows: a child spawned
    inside a test session that had leaked ``KG_COLLECTION=Alpha_KnowledgeGraph``
    into ``os.environ``.
    """
    monkeypatch.setattr(sync_mod, "COLLECTION_NAME", "Alpha_KnowledgeGraph")
    monkeypatch.setattr(sync_mod, "DEV_COLLECTION_NAME", "")
    monkeypatch.setattr(sync_mod, "SHARED_COLLECTION_NAME", "")
    monkeypatch.delenv(fcg.ALLOW_FIXTURE_WRITES_ENV, raising=False)
    with pytest.raises(SystemExit) as excinfo:
        sync_mod._refuse_fixture_shaped_targets()
    assert excinfo.value.code == 2


def test_sync_refuses_a_fixture_dev_or_shared_target(monkeypatch, sync_mod):
    """Every target the script can write, not just the KG one."""
    monkeypatch.delenv(fcg.ALLOW_FIXTURE_WRITES_ENV, raising=False)
    for kg, dev, shared in (
        ("Real_KnowledgeGraph", "Foo_Development", ""),
        ("Real_KnowledgeGraph", "", "Beta_KnowledgeGraph"),
    ):
        monkeypatch.setattr(sync_mod, "COLLECTION_NAME", kg)
        monkeypatch.setattr(sync_mod, "DEV_COLLECTION_NAME", dev)
        monkeypatch.setattr(sync_mod, "SHARED_COLLECTION_NAME", shared)
        with pytest.raises(SystemExit) as excinfo:
            sync_mod._refuse_fixture_shaped_targets()
        assert excinfo.value.code == 2


def test_sync_main_calls_the_refusal_before_connecting(
    monkeypatch, sync_mod, capsys
):
    """WIRING: ``main()`` itself refuses — the helper existing is not enough.

    Driven, not source-scanned: a call that got deleted would leave the helper
    test above perfectly green. ``WeaviateMCPServer`` is replaced with a
    detonator, so reaching the backend is a hard failure rather than a slow
    timeout against the sentinel.
    """
    def _explode(*args, **kwargs):  # pragma: no cover - must never be reached
        raise AssertionError("main() connected before refusing the target")

    monkeypatch.setattr(sync_mod, "WeaviateMCPServer", _explode)
    monkeypatch.setattr(sync_mod, "COLLECTION_NAME", "Alpha_KnowledgeGraph")
    monkeypatch.setattr(sync_mod, "DEV_COLLECTION_NAME", "")
    monkeypatch.setattr(sync_mod, "SHARED_COLLECTION_NAME", "")
    monkeypatch.setattr(sys, "argv", ["sync_knowledge_graph.py", "--all"])
    monkeypatch.delenv(fcg.ALLOW_FIXTURE_WRITES_ENV, raising=False)

    with pytest.raises(SystemExit) as excinfo:
        sync_mod.main()
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "Alpha_KnowledgeGraph" in err
    assert fcg.ALLOW_FIXTURE_WRITES_ENV in err


def test_sync_lets_a_real_target_through(monkeypatch, sync_mod):
    """The leave-alone half: a real install must be untouched by the guard."""
    monkeypatch.setattr(sync_mod, "COLLECTION_NAME", "VCODev_KnowledgeGraph")
    monkeypatch.setattr(sync_mod, "DEV_COLLECTION_NAME", "VCODev_Development")
    monkeypatch.setattr(
        sync_mod, "SHARED_COLLECTION_NAME", "VibeCodedOrchestrator_KnowledgeGraph"
    )
    monkeypatch.delenv(fcg.ALLOW_FIXTURE_WRITES_ENV, raising=False)
    sync_mod._refuse_fixture_shaped_targets()  # must not raise or exit


def _diagram_row():
    from vco_lib.diagram_indexer import DiagramRow

    return DiagramRow(
        project_id="p1",
        diagram_name="x",
        diagram_type="mermaid",
        file_path="docs/diagrams/x.mmd",
        category_path="docs/diagrams",
        enabled=1,
        inferred_title="X",
        diagram_kind="graph",
        content_text="graph TD;",
        node_count=1,
        edge_count=0,
        chat_id=None,
        linked_session_summary=None,
        config_json=None,
        created_at=0,
        updated_at=0,
    )


def test_diagram_upsert_refuses_a_fixture_collection(monkeypatch, caplog):
    """The diagram indexer's ONE write seam.

    Live evidence for this seam specifically: an empty `Foo_Diagrams` class on
    the maintainer's Weaviate — the incident's shape caught one step earlier,
    at creation, with nothing written yet.
    """
    from vco_lib import diagram_indexer

    monkeypatch.delenv(fcg.ALLOW_FIXTURE_WRITES_ENV, raising=False)

    def _explode(**kwargs):  # pragma: no cover - must never be reached
        raise AssertionError("the indexer connected to a fixture-named class")

    import weaviate  # type: ignore

    monkeypatch.setattr(weaviate, "connect_to_custom", _explode)

    with caplog.at_level("ERROR"):
        wrote = diagram_indexer._weaviate_upsert(
            _diagram_row(),
            weaviate_url="http://localhost:8081",
            collection_name="Foo_Diagrams",
        )
    assert wrote is False
    # Refused, not silently skipped: the log names the class and the escape.
    assert any(
        "Foo_Diagrams" in r.message and fcg.ALLOW_FIXTURE_WRITES_ENV in r.message
        for r in caplog.records
    ), [r.message for r in caplog.records]


def test_diagram_upsert_reaches_the_client_for_a_real_collection(monkeypatch):
    """The leave-alone half: a real project's diagrams still index."""
    from vco_lib import diagram_indexer

    monkeypatch.delenv(fcg.ALLOW_FIXTURE_WRITES_ENV, raising=False)
    reached: list[str] = []

    def _connect(**kwargs):
        reached.append("connected")
        raise RuntimeError("stop here — past the guard is all we assert")

    import weaviate  # type: ignore

    monkeypatch.setattr(weaviate, "connect_to_custom", _connect)

    with pytest.raises(RuntimeError):
        diagram_indexer._weaviate_upsert(
            _diagram_row(),
            weaviate_url="http://localhost:8081",
            collection_name="VCODev_Diagrams",
        )
    assert reached == ["connected"]


# ---------------------------------------------------------------------------
# 6. The doctor: a fixture-shaped ghost reads DIFFERENTLY from a leftover
# ---------------------------------------------------------------------------


def _unclaimed(*pairs):
    from vco_lib.kg_binding_doctor import UnclaimedClass

    return [UnclaimedClass(name=n, count=c) for n, c in pairs]


def test_doctor_labels_a_fixture_shaped_ghost():
    from vco_lib import doctor

    unclaimed = _unclaimed(("Alpha_KnowledgeGraph", 70))
    summary = doctor._kg_unclaimed_summary(unclaimed)
    assert "fixture-shaped" in summary
    assert "test or probe harness" in summary
    assert "Alpha_KnowledgeGraph (70 objects" in summary

    remedy = doctor._kg_unclaimed_remediation(unclaimed)
    assert "Fixture-shaped: Alpha_KnowledgeGraph" in remedy
    assert "no project to re-add" in remedy
    assert "VERIFY PARITY FIRST" in remedy
    # The destructive step stays a user STOP POINT: named in prose, never
    # printed as a command anyone (or anything) could run.
    assert "YOURS TO MAKE" in remedy
    for destructive in ("DELETE ", "--delete", "delete_collection", "drop-collection"):
        assert destructive not in remedy, (
            f"the unclaimed remedy must never print a destructive command "
            f"({destructive!r} found)"
        )


def test_doctor_keeps_todays_wording_for_a_non_fixture_ghost():
    from vco_lib import doctor

    unclaimed = _unclaimed(("AgapeTest_KnowledgeGraph", 84))
    summary = doctor._kg_unclaimed_summary(unclaimed)
    assert summary == (
        "1 populated KG class(es) no registered project claims: "
        "AgapeTest_KnowledgeGraph (84 objects)"
    )
    assert "fixture-shaped" not in summary

    remedy = doctor._kg_unclaimed_remediation(unclaimed)
    assert "Fixture-shaped" not in remedy
    assert "re-add it (launcher Projects" in remedy


def test_doctor_mixed_set_names_only_the_fixture_ones():
    from vco_lib import doctor

    unclaimed = _unclaimed(
        ("Alpha_KnowledgeGraph", 70), ("AgapeTest_KnowledgeGraph", 84)
    )
    summary = doctor._kg_unclaimed_summary(unclaimed)
    assert "2 populated KG class(es)" in summary
    assert "1 of them fixture-shaped" in summary
    assert "Alpha_KnowledgeGraph (70 objects, fixture-shaped)" in summary
    assert "AgapeTest_KnowledgeGraph (84 objects)" in summary

    remedy = doctor._kg_unclaimed_remediation(unclaimed)
    assert "Fixture-shaped: Alpha_KnowledgeGraph" in remedy
    assert "AgapeTest_KnowledgeGraph" in remedy


def test_deferral_entry_explains_the_fixture_case():
    from vco_lib import doctor
    from vco_lib.doctor import Finding, STATUS_PROBLEM

    unclaimed = _unclaimed(("Alpha_KnowledgeGraph", 70))
    finding = Finding(
        probe="kg_binding_evidence",
        status=STATUS_PROBLEM,
        summary=doctor._kg_unclaimed_summary(unclaimed),
        condition_id=doctor.CID_KG_UNCLAIMED,
        command=doctor._kg_unclaimed_remediation(unclaimed),
        detail={"unclaimed": [{"class": "Alpha_KnowledgeGraph", "count": 70}]},
    )
    entry = doctor._kg_unclaimed_entry(finding)
    assert "FIXTURE-SHAPED" in entry.why_deferred
    assert "Alpha_KnowledgeGraph" in entry.why_deferred
    assert entry.dismiss_fields == {"classes": ["Alpha_KnowledgeGraph"]}


def test_deferral_entry_unchanged_for_a_non_fixture_ghost():
    from vco_lib import doctor
    from vco_lib.doctor import Finding, STATUS_PROBLEM

    unclaimed = _unclaimed(("AgapeTest_KnowledgeGraph", 84))
    finding = Finding(
        probe="kg_binding_evidence",
        status=STATUS_PROBLEM,
        summary=doctor._kg_unclaimed_summary(unclaimed),
        condition_id=doctor.CID_KG_UNCLAIMED,
        command=doctor._kg_unclaimed_remediation(unclaimed),
        detail={"unclaimed": [{"class": "AgapeTest_KnowledgeGraph", "count": 84}]},
    )
    entry = doctor._kg_unclaimed_entry(finding)
    assert "FIXTURE-SHAPED" not in entry.why_deferred
    assert entry.why_deferred.startswith("This is a diagnosis, not a defect report")
