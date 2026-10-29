# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Config files in the code graph (v0.2.105): parsers, literals, link rules,
the walk, the hash gate and the generation bump.

Hermetic: recording fake collections (the golden-suite idiom), stubbed
embeddings, ``WEAVIATE_URL`` pinned by the suite's conftest. Fixtures are
small synthetic files shaped like the real formats (a ``.tweakers`` AI-tuning
file, an INI with multi-line values, JSON/JSONC) — no real project content.
"""
from __future__ import annotations

import importlib.util
import subprocess
import types
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from vco_lib import codegraph_config_refs as refs
from vco_lib import codegraph_extractor_generation as gen
from vco_lib.codegraph_lang import config as cfg

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ANALYZER_PATH = _REPO_ROOT / "templates" / "scripts" / "analyze_code_graph.py"

TWEAKERS = """AI Tweaks
Prefix groups with ====== items with > and descriptions with //

>orphan_item:7
======Passing Logic
>Pass attempt Min Time To Impact:default[3.000000]          //minimum time to impact before a pass
>AimPID_P:1.000000                                        //description missing.
>consistency_change_time (in seconds):default[0.5]
======Steering
>AimPID_I:default[0.25]
"""

INI = """# vehicle parameters
[GENERAL_OPTIONS]
ggv_file = "ggv.csv"
veh_params = {"mass": 1200.0,
              "width": 2.0}
dup_key = 1
dup_key = 2
window.width : 640

; optimisation
[OPTIMIZATION_OPTIONS]
opt_type = "mintime"
bare_flag
"""

JSON = """{
  // JSONC comment
  "veh_params": {"mass_kg": 1200, "dims": {"length_m": 4.5}},
  "frictionmap": {"coeffs": [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]},
  "layers": [{"name": "a", "scale_factor": 1}, {"name": "b", "offset_m": 2}],
  "version": 3,
}
"""


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


def test_tweakers_both_value_forms_spaces_and_root_items() -> None:
    doc = cfg.parse_config("tweakers", TWEAKERS)
    assert doc.header == "AI Tweaks"
    names = [s.name for s in doc.sections]
    assert names == [cfg.ROOT_SECTION, "Passing Logic", "Steering"]
    passing = doc.sections[1]
    keys = {k.path: k for k in passing.keys}
    k = keys["Pass attempt Min Time To Impact"]
    assert (k.value, k.default, k.description) == ("3.000000", True, "minimum time to impact before a pass")
    assert keys["AimPID_P"].value == "1.000000" and not keys["AimPID_P"].default
    assert "consistency_change_time (in seconds)" in keys
    assert passing.start_line == 5 and passing.signature == "======Passing Logic"
    assert doc.sections[0].keys[0].path == "orphan_item"


def test_tweakers_without_grammar_is_invalid() -> None:
    with pytest.raises(cfg.ConfigParseError):
        cfg.parse_config("tweakers", "just some notes\nnothing here\n")


def test_ini_sections_multiline_duplicates_and_bare_keys() -> None:
    doc = cfg.parse_config("ini", INI)
    general, optim = doc.sections
    assert general.name == "GENERAL_OPTIONS" and general.doc == "vehicle parameters"
    vals = [(k.path, k.value) for k in general.keys]
    assert ("veh_params", '{"mass": 1200.0, "width": 2.0}') in vals
    assert [v for p, v in vals if p == "dup_key"] == ["1", "2"]  # both lines kept
    assert ("window.width", "640") in vals                       # ':' separator
    assert optim.doc == "optimisation"
    assert [k.path for k in optim.keys] == ["opt_type", "bare_flag"]


def test_ini_without_section_uses_root_and_garbage_is_invalid() -> None:
    doc = cfg.parse_config("ini", "a = 1\nb: two\n")
    assert [s.name for s in doc.sections] == [cfg.ROOT_SECTION]
    with pytest.raises(cfg.ConfigParseError):
        cfg.parse_config("ini", "this is prose\nnot a config\n")


def test_json_sections_paths_previews_and_lines() -> None:
    doc = cfg.parse_config("json", JSON)
    by_name = {s.name: s for s in doc.sections}
    assert set(by_name) == {cfg.ROOT_SECTION, "veh_params", "frictionmap"}
    veh = {k.path: k for k in by_name["veh_params"].keys}
    assert veh["mass_kg"].value == "1200" and veh["dims.length_m"].value == "4.5"
    assert veh["mass_kg"].line == 3
    coeffs = by_name["frictionmap"].keys[0]
    assert coeffs.path == "coeffs"
    assert coeffs.value.endswith("(len=10)") and "…" in coeffs.value
    root = {k.path for k in by_name[cfg.ROOT_SECTION].keys}
    assert {"layers[].name", "layers[].scale_factor", "layers[].offset_m", "version"} <= root


@pytest.mark.parametrize("text", ['{"a": ', "[1, 2", "0123", '{"a": 1} trailing'])
def test_json_invalid_raises(text: str) -> None:
    with pytest.raises(cfg.ConfigParseError):
        cfg.parse_config("json", text)


def test_one_line_json_is_not_skipped_but_over_cap_is(monkeypatch: pytest.MonkeyPatch) -> None:
    one_line = '{"a": {' + ", ".join(f'"k{i}": {i}' for i in range(400)) + "}}"
    assert cfg.skip_reason(one_line, Path("x.json")) is None
    monkeypatch.setenv(cfg.MAX_BYTES_ENV, "100")
    assert "cap" in (cfg.skip_reason(one_line, Path("x.json")) or "")
    assert (cfg.skip_reason("{", Path("y.json")) or "").startswith("not valid json")


def test_key_cap_truncates_and_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(cfg.MAX_KEYS_ENV, "3")
    doc = cfg.parse_config("tweakers", TWEAKERS)
    assert doc.truncated and doc.total_keys == 5
    assert sum(len(s.keys) for s in doc.sections) == 3
    assert "truncated at 3 of 5 keys" in cfg.module_summary(doc, "a.tweakers")


@pytest.mark.parametrize("name,fmt", [
    ("a.tweakers", "tweakers"), ("b.INI", "ini"), ("c.json", "json"),
    ("package-lock.json", ""), ("npm-shrinkwrap.json", ""), ("x.lock.json", ""),
    ("x.min.json", ""), (".vco-manifest.json", ""), ("notes.descriptions", ""),
    ("readme.txt", ""),
])
def test_config_format_is_exactly_three_extensions(name: str, fmt: str) -> None:
    assert cfg.config_format(name) == fmt


def test_body_line_round_trips() -> None:
    key = cfg.ConfigKey("Pass attempt", "3.0", 1, default=True, description="min time")
    assert cfg.parse_body_line(cfg.body_line(key)) == ("Pass attempt", "3.0", True, "min time")
    assert cfg.parse_body_line("not a key line") is None


class _Helpers:
    project_name = "P"

    def __init__(self) -> None:
        self.embedded: List[str] = []

    def generate_embedding(self, text: str) -> None:
        self.embedded.append(text)


def test_extractor_rows_carry_values_in_body_and_embedding(tmp_path: Path) -> None:
    f = tmp_path / "ai" / "Driver.tweakers"
    f.parent.mkdir()
    f.write_text(TWEAKERS, encoding="utf-8")
    helpers = _Helpers()
    fx = cfg.extract_config_file(TWEAKERS, f, tmp_path, helpers)
    assert fx.module is not None and fx.module.language == "tweakers"
    assert "sections (root), Passing Logic, Steering" in fx.module.module_summary
    passing = next(e for e in fx.entities if e.name == "Passing Logic")
    assert passing.full_name == "ai/Driver.tweakers::Passing Logic"
    assert passing.extras["methods"][1] == "AimPID_P"
    assert passing.extras["language"] == "tweakers"
    assert "AimPID_P = 1.000000" in (passing.body or "")
    assert passing.deferred_embed is not None
    passing.deferred_embed()
    assert "Pass attempt Min Time To Impact = 3.000000 (default)  // minimum time" in helpers.embedded[0]


# ---------------------------------------------------------------------------
# Literals
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("language,src,expected,absent", [
    ("cpp", 'x.Add(P, "AimPID_P", 1.3f); // "InComment"\nauto r = R"d(Raw Key)d";',
     {"AimPID_P", "Raw Key"}, {"InComment"}),
    ("cpp", '/* "Block" */ g.groupName = "Passing Logic";', {"Passing Logic"}, {"Block"}),
    ("lua", 'local a = [[Long Key]] -- "LuaComment"\nb = "short_key"',
     {"Long Key", "short_key"}, {"LuaComment"}),
    ("python", 'p.get(\'GENERAL_OPTIONS\', "ggv_file")  # "PyComment"\nd = """doc_key"""',
     {"GENERAL_OPTIONS", "ggv_file", "doc_key"}, {"PyComment"}),
    ("csharp", 'var s = @"Verbatim Key"; // "CsComment"', {"Verbatim Key"}, {"CsComment"}),
    ("javascript", "const k = 'single_key'; /* 'JsComment' */", {"single_key"}, {"JsComment"}),
])
def test_string_literals_per_language(language: str, src: str, expected: set, absent: set) -> None:
    lits = set(refs.string_literals(language, src))
    assert expected <= lits
    assert not (absent & lits)


def test_multiline_string_body_is_skipped() -> None:
    src = 'a = """\nnot_a_name "Inside"\n"""\nb = "after_key"\n'
    lits = refs.string_literals("python", src)
    assert "after_key" in lits and "Inside" not in lits


# ---------------------------------------------------------------------------
# Link rules (each with an act and a leave-alone case)
# ---------------------------------------------------------------------------


def _index(files: Dict[str, str]) -> refs.ConfigIndex:
    index = refs.ConfigIndex()
    for rel, text in files.items():
        index.add(rel, cfg.parse_config(cfg.config_format(rel), text))
    return index


def _links(index: refs.ConfigIndex, lits: set, file_lits: Optional[set] = None) -> Dict[str, str]:
    found = refs.match_literals(lits, file_lits if file_lits is not None else lits,
                                index, source="m.f")
    return {x["endpoint"]: x["confidence"] for x in found}


def test_rule1_section_distinctive_high_plain_medium_generic_none() -> None:
    idx = _index({"a.tweakers": TWEAKERS, "b.ini": "[Steering]\nk=1\n[settings]\nz=1\n"})
    assert _links(idx, {"Passing Logic"}) == {"tweakers:Passing Logic": "high"}
    assert _links(idx, {"Steering"}) == {"tweakers:Steering": "medium", "ini:Steering": "medium"}
    assert _links(idx, {"settings"}) == {}


def test_rule2_cooccurrence_high_distinctive_medium_generic_none() -> None:
    idx = _index({"a.tweakers": TWEAKERS, "c.ini": "[S]\nspeed = 1\n"})
    ep = "tweakers:Passing Logic/AimPID_P"
    assert _links(idx, {"AimPID_P"}, {"AimPID_P", "Passing Logic"})[ep] == "high"
    assert _links(idx, {"AimPID_P"}, {"AimPID_P"}) == {ep: "medium"}
    assert _links(idx, {"speed"}) == {}                                # not distinctive
    assert _links(idx, {"speed"}, {"speed", "c.ini"}) == {"ini:S/speed": "high"}  # basename cooc


def test_rule2_json_leaf_and_full_path_and_ini_dots_not_split() -> None:
    idx = _index({"cfg.json": JSON, "v.ini": INI})
    assert _links(idx, {"length_m"}) == {"json:veh_params/dims.length_m": "medium"}
    assert _links(idx, {"dims.length_m"}) == {"json:veh_params/dims.length_m": "medium"}
    both = {"window.width", "GENERAL_OPTIONS"}
    assert _links(idx, {"window.width"}, both)["ini:GENERAL_OPTIONS/window.width"] == "high"
    assert "ini:GENERAL_OPTIONS/window.width" not in _links(idx, {"width"}, both | {"width"})


def test_rule3_file_reference() -> None:
    idx = _index({"params/racecar.ini": INI})
    assert _links(idx, {"racecar.ini"}) == {"ini:file:racecar.ini": "high"}
    assert _links(idx, {"params/racecar.ini"}) == {"ini:file:racecar.ini": "high"}
    assert _links(idx, {"missing.ini"}) == {}
    assert _links(idx, {".tweakers"}) == {}


@pytest.mark.parametrize("name,ok", [
    ("AimPID_P", True), ("Pass attempt", True), ("key2value", True), ("camelCase", True),
    ("plainword", False), ("ab_c", False), ("version", False), ("Name_", True),
])
def test_rule4_distinctive(name: str, ok: bool) -> None:
    assert refs.is_distinctive(name) is ok


def test_rule5_key_in_too_many_sections_is_not_linked() -> None:
    six = "".join(f"======G{i}\n>shared_key_x:1\n" for i in range(6))
    five = "".join(f"======G{i}\n>shared_key_y:1\n" for i in range(5))
    idx = _index({"six.tweakers": six, "five.tweakers": five})
    assert _links(idx, {"shared_key_x"}) == {}
    assert len(_links(idx, {"shared_key_y"})) == 5


def test_rule6_one_link_per_endpoint_and_variant_folders_collapse() -> None:
    idx = _index({f"v{i}/a.tweakers": TWEAKERS for i in range(4)})
    found = refs.match_literals(["AimPID_P", "AimPID_P"], {"AimPID_P"}, idx, source="f")
    assert [x["endpoint"] for x in found] == ["tweakers:Passing Logic/AimPID_P"]
    assert found[0]["interaction_type"] == "config" and found[0]["protocol"] == "tweakers"
    assert len(idx.definitions("tweakers:Passing Logic/AimPID_P")) == 4
    assert idx.definitions("tweakers:Passing Logic/AimPID_P")[0].value == "1.000000"


# ---------------------------------------------------------------------------
# The real analyzer over a mini repo (fake collections)
# ---------------------------------------------------------------------------


class _Data:
    def __init__(self, store: Dict[str, Dict[str, Any]]) -> None:
        self._store = store
        self.writes = 0

    def replace(self, uuid: str, **kwargs: Any) -> None:
        self.writes += 1
        self._store[str(uuid)] = kwargs

    def insert(self, uuid: str, **kwargs: Any) -> str:
        self.writes += 1
        self._store[str(uuid)] = kwargs
        return str(uuid)

    def update(self, uuid: str, **kwargs: Any) -> None:
        self.writes += 1
        self._store.setdefault(str(uuid), {}).setdefault("properties", {}).update(
            kwargs.get("properties", {}))

    def delete_by_id(self, uuid: str) -> None:
        self._store.pop(str(uuid), None)


class _Coll:
    def __init__(self, name: str) -> None:
        self.name = name
        self.store: Dict[str, Dict[str, Any]] = {}
        self.data = _Data(self.store)

    def rows(self) -> List[Dict[str, Any]]:
        return [o.get("properties", {}) for o in self.store.values()]


@pytest.fixture(scope="module")
def analyzer_mod() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("_cfg_acg", str(_ANALYZER_PATH))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _analyzer(mod: types.ModuleType, gate: Optional[Dict[str, str]] = None) -> Any:
    setattr(mod, "generate_embedding", lambda text: None)
    setattr(mod, "embed_module", lambda summary: None)
    setattr(mod, "embed_function", lambda sig, body, language="python": None)
    setattr(mod, "embed_class", lambda sig, body, methods=None, language="python": None)
    inst = mod.CodeGraphAnalyzer.__new__(mod.CodeGraphAnalyzer)
    inst.project_name = "CfgProj"
    inst.client = object()
    inst.module_cache, inst.class_cache, inst.function_cache = {}, {}, {}
    inst.module_imports, inst.visited_uuids = {}, set()
    inst._track_visited = False
    inst._current_language = inst._current_source = ""
    inst._progress_emitter = None
    inst._cfg_pdg_data = {}
    for attr, base in (("modules_collection", "CodeModule"), ("classes_collection", "CodeClass"),
                       ("functions_collection", "CodeFunction"), ("apis_collection", "CodeAPI"),
                       ("interactions_collection", "CodeInteraction")):
        setattr(inst, attr, _Coll(f"CfgProj_{base}"))
    if gate is not None:
        # The unchanged-file gate, against a recorded {path: file_hash} map —
        # what Weaviate's stored module rows would answer.
        inst._get_existing_module = lambda path, file_hash: "u" if gate.get(path) == file_hash else None
    return inst


CPP = """#include "tweak.h"
void SetupPassing(TweakGroup& g) {
    g.groupName = "Passing Logic";
    g.Add(P, "AimPID_P", 1.3f);
    g.Add(Q, "hidden_key_xyz", 2.0f);  // "Steering" in a comment is not a literal
}
void Unrelated() { log("plain text"); }
"""

PY = """import configparser
parser = configparser.ConfigParser()
GGV = parser.get('GENERAL_OPTIONS', 'ggv_file')

def load():
    return open("params/vehicle.ini").read()
"""


def _mini_repo(root: Path) -> None:
    files = {
        "data/ai/Driver.tweakers": TWEAKERS,
        "params/vehicle.ini": INI,
        "conf/app.json": JSON,
        "bin/data/Hidden.tweakers": "======Hidden Group\n>hidden_key_xyz:1\n",
        "package-lock.json": '{"lockfileVersion": 3}',
        "broken.json": '{"a": ',
        "src/ai_driver.cpp": CPP,
        "tools/run.py": PY,
    }
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")


def _config_links(inst: Any) -> Dict[tuple, str]:
    out = {}
    for r in inst.interactions_collection.rows():
        if r.get("interaction_type") == "config":
            out[(r.get("file_path"), r["endpoint"])] = r["confidence"]
    return out


def test_walk_indexes_config_files_and_links_code(analyzer_mod: types.ModuleType, tmp_path: Path) -> None:
    _mini_repo(tmp_path)
    inst = _analyzer(analyzer_mod)
    stats = inst.analyze_repository(tmp_path)
    modules = {r["path"]: r for r in inst.modules_collection.rows()}
    assert modules["data/ai/Driver.tweakers"]["language"] == "tweakers"
    assert modules["params/vehicle.ini"]["language"] == "ini"
    assert modules["conf/app.json"]["language"] == "json"
    # bin/ stays ignored for every file type; lockfile and invalid JSON are not indexed.
    assert not any(p.startswith("bin/") for p in modules)
    assert "package-lock.json" not in modules and "broken.json" not in modules
    sections = {r["full_name"]: r for r in inst.classes_collection.rows()}
    passing = sections["data/ai/Driver.tweakers::Passing Logic"]
    assert "AimPID_P = 1.000000" in passing["class_body"] and passing["language"] == "tweakers"
    assert "AimPID_P" in passing["methods"]

    links = _config_links(inst)
    assert links[("src/ai_driver.cpp", "tweakers:Passing Logic/AimPID_P")] == "high"
    assert links[("src/ai_driver.cpp", "tweakers:Passing Logic")] == "high"
    assert links[("tools/run.py", "ini:GENERAL_OPTIONS/ggv_file")] == "high"
    assert links[("tools/run.py", "ini:GENERAL_OPTIONS")] == "high"
    assert links[("tools/run.py", "ini:file:vehicle.ini")] == "high"
    # The bin/ key is not in the index, and a comment literal never links.
    assert not any("hidden_key_xyz" in ep or "Hidden" in ep for _, ep in links)
    assert ("src/ai_driver.cpp", "tweakers:Steering") not in links
    # Function-level link carries source_function; top-level python only the module.
    by_ep = {r["endpoint"]: o for o in inst.interactions_collection.store.values()
             for r in [o["properties"]] if r.get("interaction_type") == "config"}
    assert "source_function" in by_ep["tweakers:Passing Logic/AimPID_P"].get("references", {})
    assert "source_function" not in by_ep["ini:GENERAL_OPTIONS/ggv_file"].get("references", {})
    assert stats["files_analyzed"] >= 5


def test_bin_config_is_ignored_by_the_finder_and_single_file_dispatch(
    analyzer_mod: types.ModuleType, tmp_path: Path,
) -> None:
    _mini_repo(tmp_path)
    inst = _analyzer(analyzer_mod)
    inst.index_dot_claude = False
    found = {p.relative_to(tmp_path).as_posix() for p in inst._find_config_files(tmp_path)}
    assert found == {"data/ai/Driver.tweakers", "params/vehicle.ini", "conf/app.json", "broken.json"}
    from vco_lib.codegraph_row_classify import path_is_ignored
    assert path_is_ignored("bin/data/Hidden.tweakers")
    assert not path_is_ignored("data/ai/Driver.tweakers")
    assert analyzer_mod._dispatch_name_for_file(Path("x/racecar.ini")) == "config"


def test_single_file_run_of_a_config_file(analyzer_mod: types.ModuleType, tmp_path: Path) -> None:
    _mini_repo(tmp_path)
    inst = _analyzer(analyzer_mod)
    inst.analyze_repository(tmp_path, only_file=tmp_path / "params" / "vehicle.ini")
    assert [r["path"] for r in inst.modules_collection.rows()] == ["params/vehicle.ini"]


def test_no_links_across_roots(analyzer_mod: types.ModuleType, tmp_path: Path) -> None:
    primary, extra = tmp_path / "primary", tmp_path / "extra"
    (primary / "src").mkdir(parents=True)
    (extra / "data").mkdir(parents=True)
    (extra / "src").mkdir(parents=True)
    (primary / "src" / "use.py").write_text('X = "Extra_Only_Key"\n', encoding="utf-8")
    (extra / "data" / "e.ini").write_text("[ExtraSection]\nExtra_Only_Key = 9\n", encoding="utf-8")
    (extra / "src" / "use.py").write_text('Y = "Extra_Only_Key"\n', encoding="utf-8")
    inst = _analyzer(analyzer_mod)
    inst.analyze_repository(primary, extra_paths=[extra])
    rows = [r for r in inst.interactions_collection.rows() if r.get("interaction_type") == "config"]
    sources = {(r.get("project_source"), r["endpoint"]) for r in rows}
    assert sources == {(extra.resolve().as_posix(), "ini:ExtraSection/Extra_Only_Key")}


def test_hash_gate_skips_unchanged_and_relinks_on_key_change(
    analyzer_mod: types.ModuleType, tmp_path: Path,
) -> None:
    _mini_repo(tmp_path)
    (tmp_path / "src" / "late.cpp").write_text(
        'void Late() { g.Add(Z, "Late_Added_Key", 1.0f); }\n', encoding="utf-8")
    gate: Dict[str, str] = {}
    first = _analyzer(analyzer_mod, gate)
    first.analyze_repository(tmp_path)
    gate.update({r["path"]: r["file_hash"] for r in first.modules_collection.rows()})
    assert ("src/late.cpp", "tweakers:Passing Logic/Late_Added_Key") not in _config_links(first)

    # Unchanged tree: every file hash-skips, nothing is written.
    second = _analyzer(analyzer_mod, gate)
    second.analyze_repository(tmp_path)
    assert second.modules_collection.data.writes == 0
    assert second.interactions_collection.data.writes == 0

    # A key the unchanged late.cpp names is added: ONLY late.cpp (and the
    # config file) re-walk; the other code files still hash-skip.
    tw = tmp_path / "data" / "ai" / "Driver.tweakers"
    tw.write_text(TWEAKERS.replace(">AimPID_P", ">Late_Added_Key:2\n>AimPID_P"), encoding="utf-8")
    third = _analyzer(analyzer_mod, gate)
    third.analyze_repository(tmp_path)
    rewalked = {r["path"] for r in third.modules_collection.rows()}
    assert rewalked == {"data/ai/Driver.tweakers", "src/late.cpp"}
    assert _config_links(third)[("src/late.cpp", "tweakers:Passing Logic/Late_Added_Key")] == "medium"


def test_a_file_without_config_literals_keeps_its_plain_hash(tmp_path: Path) -> None:
    idx = _index({"a.tweakers": TWEAKERS})
    assert refs.link_salt("cpp", 'void f() { log("plain text"); }', idx) == ""
    assert refs.link_salt("cpp", 'void f() { g.Add(P, "AimPID_P"); }', idx).startswith("config-links:")
    assert refs.link_salt("cpp", 'void f() { g.Add(P, "AimPID_P"); }', refs.ConfigIndex()) == ""


def test_reconcile_judges_interaction_rows(analyzer_mod: types.ModuleType, tmp_path: Path,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
    """A link to a key that was removed must be deleted when its code file
    re-walks: the per-file reconcile now covers CodeInteraction rows."""
    _mini_repo(tmp_path)
    seen: List[Any] = []

    def _capture(collections, walked, **kw):
        seen.append([getattr(c, "name", "") for c, _p in collections])
        return 0, 0

    monkeypatch.setattr(analyzer_mod, "_reconcile_walked_file_rows", _capture)
    inst = _analyzer(analyzer_mod)
    inst.analyze_repository(tmp_path)
    assert seen and "CfgProj_CodeInteraction" in seen[0]


# ---------------------------------------------------------------------------
# Incremental re-queue + the generation bump
# ---------------------------------------------------------------------------


class _Ctx:
    def __init__(self, cfg_files: List[Path]) -> None:
        self._files = cfg_files

    def _find_files_for(self, key: str, root: Path) -> List[Path]:
        assert key == "config"
        return list(self._files)


def _git(changed: List[str], deleted: List[str]):
    def run(cmd, cwd=None, **kw):
        if "rev-parse" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "abc\n", "")
        if "--name-only" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "\n".join(changed) + "\n", "")
        out = "".join(f"D\0{d}\0" for d in deleted)
        return subprocess.CompletedProcess(cmd, 0, out, "")
    return run


def test_requeue_adds_unchanged_code_files_when_a_config_file_changed(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    cfg_file = tmp_path / "a.ini"
    code = [tmp_path / "x.py", tmp_path / "y.py"]
    ctx = _Ctx([cfg_file])
    out = refs.requeue_on_config_change(ctx, tmp_path, "python", code, [code[0]], None,
                                        run=_git(["a.ini"], []))
    assert out == code


def test_requeue_leaves_files_alone_when_no_config_changed(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    code = [tmp_path / "x.py", tmp_path / "y.py"]
    ctx = _Ctx([tmp_path / "a.ini"])
    out = refs.requeue_on_config_change(ctx, tmp_path, "python", code, [code[0]], None,
                                        run=_git(["x.py"], []))
    assert out == [code[0]]
    # and the config language itself is never re-queued
    assert refs.requeue_on_config_change(_Ctx([]), tmp_path, "config", code, [], None,
                                         run=_git(["a.ini"], [])) == []


def test_requeue_on_a_deleted_config_file(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    code = [tmp_path / "x.py"]
    out = refs.requeue_on_config_change(_Ctx([]), tmp_path, "python", code, [], None,
                                        run=_git([], ["gone.tweakers"]))
    assert out == code


def test_generation_bump_requeues_graphs_built_before_config_links() -> None:
    assert gen.CURRENT_EXTRACTOR_GENERATION == "0.2.105"
    assert "config" in gen.EXTRACTOR_GENERATION_RUNG_REASONS["0.2.105"]
    assert not gen.generation_is_current("0.2.102")
    assert gen.generation_is_current("0.2.105")


def _git_commit(root: Path, msg: str) -> str:
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
           "PATH": "/usr/bin:/bin"}
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, env=env, capture_output=True)
    subprocess.run(["git", "commit", "-qm", msg], cwd=root, check=True, env=env, capture_output=True)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True, env=env,
                          capture_output=True, text=True).stdout.strip()


def test_incremental_walk_relinks_unchanged_code_after_a_config_commit(
    analyzer_mod: types.ModuleType, tmp_path: Path,
) -> None:
    """The real incremental path: only the .tweakers file is in the git range,
    yet the unchanged code file that names the new key re-walks and links."""
    if subprocess.run(["git", "--version"], capture_output=True).returncode != 0:
        pytest.skip("git unavailable")
    _mini_repo(tmp_path)
    (tmp_path / "src" / "late.cpp").write_text(
        'void Late() { g.Add(Z, "Late_Added_Key", 1.0f); }\n', encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    base = _git_commit(tmp_path, "base")
    gate: Dict[str, str] = {}
    first = _analyzer(analyzer_mod, gate)
    first.analyze_repository(tmp_path)
    gate.update({r["path"]: r["file_hash"] for r in first.modules_collection.rows()})

    tw = tmp_path / "data" / "ai" / "Driver.tweakers"
    tw.write_text(TWEAKERS.replace(">AimPID_P", ">Late_Added_Key:2\n>AimPID_P"), encoding="utf-8")
    _git_commit(tmp_path, "add a key")
    inc = _analyzer(analyzer_mod, gate)
    inc.analyze_repository(tmp_path, incremental=True, since_commit=base)
    assert {r["path"] for r in inc.modules_collection.rows()} == {
        "data/ai/Driver.tweakers", "src/late.cpp"}
    assert ("src/late.cpp", "tweakers:Passing Logic/Late_Added_Key") in _config_links(inc)


def test_parsers_dialog_never_offers_a_grammar_for_config_files(tmp_path: Path) -> None:
    """Config formats are parsed in pure Python: the language scan that drives
    the "install parsers" dialog must not report them as languages."""
    from vco_lib.codegraph_parsers import scan_project

    for name in ("a.json", "b.ini", "c.tweakers"):
        (tmp_path / name).write_text("{}" if name.endswith("json") else "[s]\nk=1\n", encoding="utf-8")
    languages, _dirs = scan_project(tmp_path)
    assert languages == set()
