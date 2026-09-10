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


def _files_naming(class_name: str) -> set[str]:
    """Which test files write *class_name* down — so a failure can name them."""
    hits = set()
    for path in TESTS_DIR.rglob("*.py"):
        if class_name in path.read_text(encoding="utf-8", errors="replace"):
            hits.add(path.name)
    return hits


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


#: Stems that appear in `tests/` as `<Stem>_<Family>` and are DELIBERATELY not
#: in the table. Two kinds, and the distinction is the whole point:
#:
#:   * REAL names the suite legitimately writes down — the shipped shared
#:     default, the maintainer's own project, historical spellings. Refusing
#:     these would refuse the product.
#:   * GENERIC words a real user's folder plausibly IS (`Demo`, `Test`,
#:     `Project`, `Shared`, `Widget`, `MyApp`, single letters, …). The guard's
#:     own failure mode is refusing a real install, so a stem this ordinary
#:     stays out even though the suite uses it as a fixture.
#:
#: This inventory is a TRIPWIRE, not a blessing: it is pinned so that a stem
#: appearing for the FIRST time fails the test below, naming the stem and the
#: file, and forcing the author to choose — table (it is a fixture) or here
#: (with the reason it cannot be). One paragraph of reasoning for a bucket is
#: honest; 200 hand-written per-stem reasons would be a rubber stamp.
_UNTABLED_STEMS_SNAPSHOT = frozenset({
    "1foo", "_ProjectA", "A", "Absent", "ACME_widget", "ACME_widget_old",
    "ACME_widget_team", "AcmeHospitality", "ACMERoot", "ACMEShared",
    "ACMEWidget", "AcmeWidget", "ACMEWIDGET", "ACMEWidgetArchive",
    "ACMEWidgetKnowledgeGraph", "ACMEWidgetOld", "ACMEWidgetry",
    "ACMEWidgetShell", "ACMEWidgetTeam", "Actual", "AgapeTest",
    "Alphabet", "AlphaBeta", "AmbientProject", "Anything", "ArcAgi",
    "B", "Bazquux", "Big", "Bystander", "C", "Canon",
    "CanonicalShared", "ClaudeOrchestrator", "Client_b_portal", "ClientA",
    "ClientAlpha", "ClientApp", "CsRoute", "Cur", "Decoy", "Demo",
    "Demo_project", "Drifted", "Dst", "E2EProject", "Empty", "EnvOnly",
    "EnvProj", "EnvSaid", "Existing", "ExplicitOverride", "ExplicitProj",
    "Fallback", "Foo_Bar", "ForkBrand", "Fresh", "FreshCreate", "FromDb",
    "FromEnv", "G", "Ghost_Prefix", "GhostA", "GhostB", "Gone",
    "GuardPrefix", "Hub", "Hub_Shared", "HubSaid", "ImageDataset", "Kept",
    "Leftover", "Legacy", "Legacy_prefix", "LegacyPeer", "Legacypeera",
    "Legacypeerb", "LegacyShape", "Live", "LiveName",
    "migrate_collections_partial_failure_Foo",
    "migrate_collections_partial_failure_X", "Mine", "Missing",
    "MissingPeer", "My", "My_Cool_App", "MyAlpha", "MyApp", "Myapp",
    "MyCoolApp", "MyCustom", "MyKG", "MyOrch", "MyProj", "Myproject",
    "MyProject", "MyTest", "New_Name", "NewName", "Nobody",
    "NonexistentPeer", "NotYetCreated", "NoValidUntil", "Old", "Old_Name",
    "OldCanonical", "OldName", "OldProject", "Operator",
    "OperatorsOwnProject", "Orchestrator", "Orchestrator_root",
    "OrchestratorFixtureProj", "Other", "OtherProj", "OtherProject", "P",
    "ParityFixture", "ParityTest", "Pasted", "Peer", "Peer1", "Peer2",
    "PeerOne", "PeerTwo", "Populated", "PostRename", "PreExisting",
    "Prefixexampleorchestrator", "PrefixExampleOrchestrator", "PreRename",
    "Proj", "Proj_Backend", "ProjCodeless", "Project", "ProjectA", "Q",
    "Quuux", "R", "Real", "RealName", "Recloned", "Registered", "Renamed",
    "RePicked", "ResidueProj", "RlTest", "Same", "Sample",
    "schema_reingest_incomplete_P1", "Shared", "SimRaceTest_AI",
    "SimRaceTestAI", "Small", "Snapshotted", "SoftFail", "Solo",
    "Someone_Elses", "SomeOther", "SomeProject", "Src", "Synthetic", "T",
    "TargetProject", "TeamWide", "Test", "TestInstall", "TestProj",
    "TestProject", "Third", "TP", "TProj", "TypoName", "Unrelated",
    "V0243Test", "V0289DualTest", "V0289Proj", "V0289Shared", "V0292D17",
    "V0292T", "V0292T_Shared", "V0292WPB1", "Vco_v0243_A_install",
    "Vco_v0243_B_rust", "Vco_v0243_C_cleanup", "VcoD2Scratch", "VCODev",
    "Vcodev", "vcodev", "VcoDev", "vct", "Vct_coordination",
    "VCT_transcrypt", "VctMigrateTest", "Vibecoded_orchestrator",
    "VibeCoded_Orchestrator", "Vibecodedorchestrator",
    "VibecodedOrchestrator", "VibeCodedOrchestrator", "VibeCodedTools",
    "VideoFrames", "W3Tag", "W3Tag_Shared", "W8Parity", "W8Parity_Shared",
    "WDGT", "Weirdproject", "WeirdProject", "WhiteLabel", "Widget",
    "WireProj", "WrongName", "X", "Y", "Z", "Zombie", "Zzz",
})


def test_no_untabled_fixture_stem_appears_without_a_decision():
    """MEDIUM-1: coverage runs BOTH ways.

    `test_every_table_entry_is_grounded_in_the_test_corpus` proves nothing
    invented got IN. This proves nothing new slipped PAST: a `<Stem>_<Family>`
    literal whose stem is neither in the table nor in the pinned inventory
    fails here, named, with the files that introduced it.
    """
    used = _class_names_used_by_tests()
    families = "|".join(re.escape(f) for f in fcg.COLLECTION_FAMILY_SUFFIXES)
    pattern = re.compile(rf"\A([A-Za-z0-9_]+)_(?:{families})\Z")

    tabled = {n.casefold() for n in fcg.FIXTURE_PROJECT_NAMES}
    known = tabled | {n.casefold() for n in _UNTABLED_STEMS_SNAPSHOT}

    undecided: dict[str, set[str]] = {}
    for class_name in used:
        match = pattern.match(class_name)
        if not match:
            continue
        stem = match.group(1)
        if stem.casefold() in known:
            continue
        undecided.setdefault(stem, set()).update(_files_naming(class_name))

    assert not undecided, (
        "new `<Stem>_<Family>` literal(s) in tests/ that no one has classified:\n"
        + "\n".join(
            f"  {stem} — {', '.join(sorted(files))}"
            for stem, files in sorted(undecided.items())
        )
        + "\n\nAdd the stem to vco_lib.fixture_class_guard.FIXTURE_PROJECT_NAMES "
          "if it is a fixture name (the guard will then refuse writes to "
          "`<stem>_*`), or to _UNTABLED_STEMS_SNAPSHOT if it is a real name or "
          "a word a user's project could plausibly be called."
    )


def test_the_inventory_holds_no_stem_the_tests_stopped_using():
    """The inventory is a snapshot, so it must not rot into fiction either."""
    used_stems = set()
    families = "|".join(re.escape(f) for f in fcg.COLLECTION_FAMILY_SUFFIXES)
    pattern = re.compile(rf"\A([A-Za-z0-9_]+)_(?:{families})\Z")
    for class_name in _class_names_used_by_tests():
        match = pattern.match(class_name)
        if match:
            used_stems.add(match.group(1).casefold())

    stale = sorted(
        s for s in _UNTABLED_STEMS_SNAPSHOT if s.casefold() not in used_stems
    )
    assert not stale, (
        f"_UNTABLED_STEMS_SNAPSHOT names stems no test uses any more: {stale}. "
        f"Drop them — a snapshot that keeps dead entries stops being evidence."
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

    def get(self, name: str):  # pragma: no cover - must never be reached
        self.requested.append(name)
        raise AssertionError(f"guard let a write through to {name!r}")

    def create(self, name: str = "", **kwargs):
        self.created.append(name)
        return True

    def exists(self, name):
        return False


class _RecordingClient:
    def __init__(self) -> None:
        self.collections = _RecordingCollections()


@pytest.fixture
def mcp_server_module():
    """``weaviate_mcp.server``, with a check that it came from THIS checkout.

    The path pin itself lives in ``tests/conftest.py`` (W-SHADOW) — one home,
    applied before any test module imports. This only re-states the OUTCOME at
    the point of use, because a guard test that silently measured the
    maintainer's other clone would pass or fail for reasons unrelated to the
    change under test. ``tests/test_v0294_weaviate_mcp_imports_from_the_
    checkout.py`` is the canary that pins the mechanism.
    """
    mod = importlib.import_module("weaviate_mcp.server")
    where = getattr(mod, "__file__", None)
    assert isinstance(where, str) and Path(where).is_relative_to(REPO_ROOT), (
        f"loaded {where} instead of this checkout's copy — the test would be "
        f"measuring a different tree"
    )
    return mod


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


@pytest.fixture(scope="module")
def analyzer_mod() -> types.ModuleType:
    path = REPO_ROOT / "templates" / "scripts" / "analyze_code_graph.py"
    spec = importlib.util.spec_from_file_location(
        "_v0294_fixture_guard_analyzer", str(path)
    )
    if spec is None or spec.loader is None:  # pragma: no cover
        pytest.skip(f"cannot load the analyzer from {path}")
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except SystemExit:  # pragma: no cover - weaviate-client absent
        pytest.skip("weaviate-client unavailable — analyzer cannot be loaded")
    return mod


def _fake_analyzer(analyzer_mod, client):
    obj = analyzer_mod.CodeGraphAnalyzer.__new__(analyzer_mod.CodeGraphAnalyzer)
    obj.client = client
    obj.weaviate_url = "http://localhost:8081"
    return obj


def test_analyzer_connects_to_the_env_resolved_url(monkeypatch, analyzer_mod):
    """HIGH-2: the analyzer was hardcoded to `localhost:8081`.

    `self.weaviate_url` was a label the connection ignored, so neither
    `$WEAVIATE_URL` nor the suite's pin could steer it — and the two suites
    that spawn the real analyzer minted `<Project>_Code*` classes on whatever
    instance was listening (the maintainer's live schema went 161 -> 166 while
    a reviewer watched). BOTH seams are recorded here, so nothing connects to
    anything: the assertion is which target the analyzer aimed at.
    """
    monkeypatch.setenv("WEAVIATE_URL", "http://127.0.0.9:9998")
    aimed: list[object] = []

    monkeypatch.setattr(
        analyzer_mod._wh, "connect_v4",
        lambda url=None, **kw: aimed.append(url) or object(),
    )
    # The direct-dial seam is patched on the PACKAGE, not on an attribute of
    # the analyzer module: since the analyzer connects through
    # `vco_lib.weaviate_helpers` it no longer imports the bare package at
    # all, and a module-attribute patch would only prove the attribute
    # exists. Patching the package catches any direct dial, however reached.
    weaviate_pkg = pytest.importorskip("weaviate")
    monkeypatch.setattr(
        weaviate_pkg, "connect_to_custom",
        lambda **kw: aimed.append(f"{kw.get('http_host')}:{kw.get('http_port')}")
        or object(),
    )

    analyzer = analyzer_mod.CodeGraphAnalyzer("SomeProject")
    assert analyzer.weaviate_url == "http://127.0.0.9:9998", (
        f"the constructor ignored $WEAVIATE_URL: {analyzer.weaviate_url}"
    )
    assert analyzer.connect() is True
    assert aimed == ["http://127.0.0.9:9998"], (
        f"connect() aimed at {aimed} — not the env-resolved target"
    )


def test_analyzer_refuses_to_create_a_fixture_code_class(monkeypatch, analyzer_mod):
    """Its ONE create chokepoint routes through the shared guarded create."""
    monkeypatch.delenv(fcg.ALLOW_FIXTURE_WRITES_ENV, raising=False)
    client = _RecordingClient()
    analyzer = _fake_analyzer(analyzer_mod, client)
    with pytest.raises(fcg.FixtureClassWriteRefused):
        analyzer._create_class_with_retry("Foo_CodeModule")
    assert client.collections.created == []


def test_analyzer_still_creates_a_real_code_class(monkeypatch, analyzer_mod):
    """The leave-alone half, driven — the guard must not break the analyzer."""
    monkeypatch.delenv(fcg.ALLOW_FIXTURE_WRITES_ENV, raising=False)
    client = _RecordingClient()
    analyzer = _fake_analyzer(analyzer_mod, client)
    assert analyzer._create_class_with_retry("VCODev_CodeModule") is True
    assert client.collections.created == ["VCODev_CodeModule"]


def test_project_init_refuses_a_fixture_class_at_add_time(monkeypatch):
    """MEDIUM-2: the ADD-TIME create path, so the install cannot be half-done.

    Before this, a project named for a fixture got its classes created here and
    then hit `exit 2` on every kg-sync — created but unwritable. One decision,
    at add time, with the documented escape.
    """
    from vco_lib import project_init

    monkeypatch.delenv(fcg.ALLOW_FIXTURE_WRITES_ENV, raising=False)
    monkeypatch.setattr(project_init, "_fetch_schema", lambda *a, **k: None)

    posted: list = []
    monkeypatch.setattr(
        project_init, "_http_request",
        lambda *a, **k: posted.append(a) or (200, b""),
    )

    with pytest.raises(fcg.FixtureClassWriteRefused):
        project_init._create_class(
            {"class": "Beta_KnowledgeGraph"}, weaviate_url="http://127.0.0.1:9"
        )
    assert posted == [], "the class was POSTed despite the refusal"


def test_project_init_creates_a_real_projects_class(monkeypatch):
    """The leave-alone half — a real add must be untouched."""
    from vco_lib import project_init

    monkeypatch.delenv(fcg.ALLOW_FIXTURE_WRITES_ENV, raising=False)
    monkeypatch.setattr(project_init, "_fetch_schema", lambda *a, **k: None)
    posted: list = []
    monkeypatch.setattr(
        project_init, "_http_request",
        lambda *a, **k: posted.append(a) or (200, b""),
    )

    project_init._create_class(
        {"class": "VCODev_KnowledgeGraph"}, weaviate_url="http://127.0.0.1:9"
    )
    assert len(posted) == 1


# NOTE: `install.py::_ensure_collections`' create loop is driven in
# `tests/test_install_shared_containers.py::EnsureCollectionsFixtureGuardTests`
# — that file already owns the mock-Weaviate harness, and a second copy of it
# here would be the duplication this repo's own rule forbids.


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


def test_doctor_names_fixture_shaped_classes_in_every_family():
    """LOW-1: the ownership analysis is KG-scoped, so these were invisible.

    `Foo_Diagrams` (empty, live on the maintainer's box) and a fixture-shaped
    code family need no ownership analysis — the stem is a name no project
    has — so they are named directly.
    """
    from vco_lib import doctor

    extra = ["Foo_Diagrams", "Gamma_CodeModule"]
    summary = doctor._kg_unclaimed_summary(
        _unclaimed(("AgapeTest_KnowledgeGraph", 84)), extra
    )
    assert "fixture-shaped class(es) in other families" in summary
    assert "Foo_Diagrams" in summary and "Gamma_CodeModule" in summary

    remedy = doctor._kg_unclaimed_remediation(
        _unclaimed(("AgapeTest_KnowledgeGraph", 84)), extra
    )
    assert "Fixture-shaped: Foo_Diagrams, Gamma_CodeModule" in remedy

    # And with NOTHING unclaimed, a fixture-shaped class still reports.
    only = doctor._kg_unclaimed_summary([], ["Foo_Diagrams"])
    assert only.startswith("1 fixture-shaped class(es)")


# The LEAVE-ALONE half of LOW-1 — "an OWNED class is owned, whatever its stem
# looks like" — is driven in
# `tests/test_v0292_unclaimed_kg_classes.py::test_clean_machine_ok_finding_
# names_zero_unclaimed`, which binds `Acme_KnowledgeGraph` (an in-table stem)
# to a REGISTERED project and asserts the doctor stays OK. That file owns the
# FakeMachine harness; it went red on the first cut of this feature and green
# once the scan started subtracting owned/claimed classes.


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
