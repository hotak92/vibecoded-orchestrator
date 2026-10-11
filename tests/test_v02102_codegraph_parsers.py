# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Code-graph parsers on demand (v0.2.102 W6-L): detection, offers, the
"Not now" rule, the ONE installer helper (fake pip), install.py routing, the
optional grammars' loader contract and the regex-fallback hash salt.

Nothing here installs a package, touches the network or writes outside
``tmp_path`` (the state file is redirected through ``VCT_STATE_DIR``)."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import List, Sequence, Tuple

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from vco_lib import codegraph_grammars, codegraph_parsers as cp  # noqa: E402
from vco_lib import codegraph_defs  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path / "vct"))


def _empty_state() -> dict:
    return {"version": cp.STATE_VERSION, "projects": {}, "declined": {}}


def _missing(*grammars: str):
    """``loads`` fake: every grammar loads except ``grammars``."""
    return lambda g: g not in grammars


def _offer_ids(report) -> List[str]:
    return [o.id for o in report.offers]


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def test_blank_project_then_language_added_then_offered(tmp_path) -> None:
    proj = tmp_path / "blank"
    proj.mkdir()
    state = _empty_state()
    kw = dict(loads=_missing("powershell"), installed=lambda p: False)
    first = cp.compute_offers([proj], state, **kw)
    assert first.offers == [] and first.languages[str(proj.resolve())] == []
    (proj / "scripts").mkdir()
    (proj / "scripts" / "deploy.ps1").write_text("function A { }\n")
    second = cp.compute_offers([proj], state, **kw)
    assert _offer_ids(second) == ["powershell"]
    assert second.offers[0].languages == ["powershell"]
    assert second.offers[0].projects == [str(proj.resolve())]


def test_detection_uses_the_code_graph_ignore_rules(tmp_path) -> None:
    proj = tmp_path / "p"
    for d in ("node_modules/x", "target", ".claude/hooks", "src"):
        (proj / d).mkdir(parents=True)
    (proj / "node_modules/x/a.proto").write_text("")
    (proj / "target/b.ps1").write_text("")
    (proj / ".claude/hooks/hook.ps1").write_text("")  # VCO's own hooks: not the user's code
    (proj / "src/x.min.js").write_text("")
    (proj / "src/main.rs").write_text("fn main() {}\n")
    langs, _dirs = cp.scan_project(proj)
    assert langs == {"rust"}


def test_signature_cache_skips_the_scan_until_a_directory_changes(tmp_path, monkeypatch) -> None:
    proj = tmp_path / "p"
    (proj / "a" / "b").mkdir(parents=True)
    (proj / "a" / "b" / "m.go").write_text("package m\n")
    state = _empty_state()
    assert cp.project_languages(proj, state) == ["go"]
    real_scan = cp.scan_project
    monkeypatch.setattr(cp, "scan_project", lambda root: pytest.fail("rescanned an unchanged project"))
    assert cp.project_languages(proj, state) == ["go"]  # cached: no scan
    monkeypatch.setattr(cp, "scan_project", real_scan)
    deep = proj / "a" / "b" / "api.proto"
    deep.write_text('syntax = "proto3";\n')
    os.utime(proj / "a" / "b", ns=(1, 1))  # a coarse-mtime filesystem must still see a change
    assert cp.project_languages(proj, state) == ["go", "proto"]


# ---------------------------------------------------------------------------
# Offers: already-installed never offered; broken never offered
# ---------------------------------------------------------------------------


def _ps_and_proto_project(tmp_path) -> Path:
    proj = tmp_path / "mixed"
    proj.mkdir()
    (proj / "a.ps1").write_text("function A { }\n")
    (proj / "b.proto").write_text('syntax = "proto3";\n')
    return proj


def test_already_installed_parsers_are_never_offered(tmp_path) -> None:
    proj = _ps_and_proto_project(tmp_path)
    report = cp.compute_offers([proj], _empty_state(), loads=lambda g: True,
                               installed=lambda p: False)
    assert report.offers == []


def test_installed_at_pin_but_not_loading_is_reported_not_offered(tmp_path) -> None:
    proj = _ps_and_proto_project(tmp_path)
    report = cp.compute_offers([proj], _empty_state(), loads=_missing("powershell"),
                               installed=lambda p: p == "tree-sitter-powershell")
    assert report.offers == []
    assert [b["id"] for b in report.broken] == ["powershell"]


def test_pack_installed_but_grammar_not_downloaded_is_offered(tmp_path) -> None:
    proj = _ps_and_proto_project(tmp_path)
    report = cp.compute_offers([proj], _empty_state(), loads=_missing("proto"),
                               installed=lambda p: True, pack_on_disk=lambda g: False)
    assert _offer_ids(report) == ["proto"]


def test_missing_default_grammar_is_offered_too(tmp_path) -> None:
    proj = tmp_path / "rs"
    proj.mkdir()
    (proj / "lib.rs").write_text("fn a() {}\n")
    report = cp.compute_offers([proj], _empty_state(), loads=_missing("rust"),
                               installed=lambda p: False)
    assert _offer_ids(report) == ["rust"]
    assert report.offers[0].optional is False


# ---------------------------------------------------------------------------
# "Not now"
# ---------------------------------------------------------------------------


def test_not_now_is_remembered_until_a_new_language_appears(tmp_path) -> None:
    proj = _ps_and_proto_project(tmp_path)
    state = _empty_state()
    kw = dict(loads=_missing("powershell", "proto"), installed=lambda p: False,
              pack_on_disk=lambda g: False)
    assert _offer_ids(cp.compute_offers([proj], state, **kw)) == ["powershell", "proto"]
    cp.record_decline(state, ["powershell", "proto"], [proj], loads=kw["loads"])
    assert cp.compute_offers([proj], state, **kw).offers == []  # no nag
    # A DIFFERENT project gains PowerShell -> PowerShell is offered again,
    # proto (no new need) is not.
    other = tmp_path / "other"
    other.mkdir()
    (other / "x.psm1").write_text("function B { }\n")
    report = cp.compute_offers([proj, other], state, **kw)
    assert _offer_ids(report) == ["powershell"]
    assert report.offers[0].projects == sorted([str(proj.resolve()), str(other.resolve())])


def test_installing_clears_the_decline(tmp_path) -> None:
    state_file = tmp_path / "state.json"
    state = _empty_state()
    state["declined"]["powershell"] = [["/x", "powershell"]]
    cp.save_state(state, state_file)
    result = cp.install_parsers(["powershell"], python="py", runner=_FakePip().run,
                                state_file=state_file, log_path=tmp_path / "log")
    assert result.ok
    assert "powershell" not in cp.load_state(state_file)["declined"]


# ---------------------------------------------------------------------------
# The installer helper (fake pip)
# ---------------------------------------------------------------------------


class _FakePip:
    def __init__(self, pip_rc: int = 0, verify: dict | None = None, raise_on: str = "") -> None:
        self.pip_rc = pip_rc
        self.verify = verify
        self.raise_on = raise_on
        self.calls: List[Tuple[List[str], int]] = []

    def run(self, argv: Sequence[str], timeout: int) -> Tuple[int, str, str]:
        argv = list(argv)
        self.calls.append((argv, timeout))
        is_pip = argv[1:3] == ["-m", "pip"]
        if self.raise_on == ("pip" if is_pip else "verify"):
            raise subprocess.TimeoutExpired(argv, timeout)
        if is_pip:
            return self.pip_rc, "pip out", "ERROR: no matching distribution" if self.pip_rc else ""
        ids = argv[argv.index("--parsers") + 1].split(",")
        verdict = self.verify if self.verify is not None else {p: "ok" for p in ids}
        return (0 if all(v == "ok" for v in verdict.values()) else 1,
                json.dumps({"parsers": verdict}), "")


def test_install_runs_pip_with_exactly_the_pinned_specs_then_verifies(tmp_path) -> None:
    pip = _FakePip()
    log = tmp_path / "parsers.log"
    result = cp.install_parsers(["powershell", "proto"], python="/venv/bin/python",
                                runner=pip.run, state_file=tmp_path / "s.json", log_path=log)
    assert result.ok, result
    assert result.installed == ["powershell", "proto"]
    pip_argv, _t = pip.calls[0]
    assert pip_argv[:4] == ["/venv/bin/python", "-m", "pip", "install"]
    specs = [a for a in pip_argv if "==" in a]
    assert specs == ["tree-sitter == 0.26.0", "tree-sitter-powershell == 0.26.4",
                     "tree-sitter-language-pack == 1.21.3"]
    verify_argv, _t = pip.calls[1]
    assert verify_argv == ["/venv/bin/python", "-m", "vco_lib.codegraph_parsers", "verify",
                           "--parsers", "powershell,proto"]
    assert "pip exit 0" in log.read_text()


def test_pip_failure_is_loud_and_logged(tmp_path) -> None:
    log = tmp_path / "parsers.log"
    result = cp.install_parsers(["powershell"], python="py", runner=_FakePip(pip_rc=1).run,
                                state_file=tmp_path / "s.json", log_path=log)
    assert not result.ok
    assert "pip exited 1" in result.message
    assert "no matching distribution" in result.log_tail
    assert result.log_path == str(log)
    assert "pip exit 1" in log.read_text()


def test_a_grammar_that_does_not_load_after_pip_is_a_failure(tmp_path) -> None:
    pip = _FakePip(verify={"proto": "DownloadError: Failed to fetch manifest"})
    result = cp.install_parsers(["proto"], python="py", runner=pip.run,
                                state_file=tmp_path / "s.json", log_path=tmp_path / "l")
    assert not result.ok
    assert result.failed == [{"id": "proto", "error": "DownloadError: Failed to fetch manifest"}]


def test_a_hung_pip_is_reported_not_raised(tmp_path) -> None:
    result = cp.install_parsers(["powershell"], python="py", runner=_FakePip(raise_on="pip").run,
                                state_file=tmp_path / "s.json", log_path=tmp_path / "l")
    assert not result.ok and "timed out" in result.message


def test_unknown_parser_is_refused_before_pip(tmp_path) -> None:
    pip = _FakePip()
    result = cp.install_parsers(["cobol"], python="py", runner=pip.run,
                                state_file=tmp_path / "s.json", log_path=tmp_path / "l")
    assert not result.ok and pip.calls == []


def test_requirement_specs_come_from_pyproject() -> None:
    assert cp.requirement_specs(["powershell"]) == [
        "tree-sitter == 0.26.0", "tree-sitter-powershell == 0.26.4"]
    assert cp.requirement_specs(["rust"])[1] == "tree-sitter-rust == 0.24.2"
    with pytest.raises(ValueError):
        cp.requirement_specs(["nope"])


def test_catalogue_covers_every_grammar_once() -> None:
    cat = cp.catalogue()
    grammars = [g for p in cat.values() for g in p.grammars]
    assert sorted(grammars) == sorted(codegraph_grammars.all_grammar_ids())
    assert {p.id for p in cat.values() if p.optional} == {"powershell", "proto"}
    assert cat["typescript"].grammars == ("typescript", "tsx")


def test_install_py_routes_the_default_extra_through_the_helper(monkeypatch, tmp_path) -> None:
    """install.py keeps installing the default extra, through the ONE helper
    (mutating the helper's ``install`` proves the call is real, not a string)."""
    import install as installer

    seen = {}

    def fake_install(**kw):
        seen.update(kw)
        return cp.InstallResult(True, "ok", installed=["x"])

    monkeypatch.setattr(cp, "install", fake_install)
    monkeypatch.delenv("VCT_SKIP_CODEGRAPH_TS", raising=False)
    monkeypatch.setattr(installer, "_log_install_event", lambda *a, **k: None)
    installer._install_codegraph_treesitter(Path("/venv/bin/python"))
    assert seen["python"] == Path("/venv/bin/python")
    assert seen["targets"] == ["-e", f"{installer.PROJECT_ROOT}[codegraph-ts]"]
    assert not seen.get("verify_parsers")  # the default extra: pip only, as before
    assert "--prefer-binary" in seen["pip_flags"]


# ---------------------------------------------------------------------------
# Optional grammars: the loader never downloads; regex fallback; hash salt
# ---------------------------------------------------------------------------


class _FakePack:
    def __init__(self, downloaded: List[str]) -> None:
        self.downloaded = downloaded
        self.configured = None
        self.get_calls: List[str] = []

    class PackConfig:  # noqa: D106
        def __init__(self, cache_dir=None):
            self.cache_dir = cache_dir

    def configure(self, cfg):
        self.configured = cfg.cache_dir

    def downloaded_languages(self):
        return list(self.downloaded)

    def get_language(self, name):
        self.get_calls.append(name)
        raise AssertionError("get_language must not be reached for an absent grammar")

    def download(self, names):
        self.downloaded.extend(names)


def test_a_pack_grammar_not_on_disk_is_unavailable_and_never_downloaded(monkeypatch) -> None:
    pytest.importorskip("tree_sitter")
    pack = _FakePack(downloaded=[])
    monkeypatch.setattr(codegraph_grammars.importlib, "import_module",
                        lambda name: pack if name == codegraph_grammars.PACK_MODULE else __import__(name))
    monkeypatch.setattr(codegraph_grammars, "_LANGUAGES", {})
    assert codegraph_grammars.load_language("proto") is None
    assert pack.get_calls == [] and pack.downloaded == []
    assert pack.configured == str(codegraph_grammars.pack_cache_base())


def test_install_time_download_targets_the_venv_cache(monkeypatch) -> None:
    pack = _FakePack(downloaded=[])
    monkeypatch.setattr(codegraph_grammars.importlib, "import_module", lambda name: pack)
    codegraph_grammars.download_pack_grammars(["proto", "powershell"])
    assert pack.downloaded == ["proto"]  # only pack grammars
    assert str(pack.configured).startswith(sys.prefix)


def test_regex_fallback_salt_follows_grammar_availability(monkeypatch) -> None:
    monkeypatch.setattr(codegraph_grammars, "_LANGUAGES", {"powershell": None, "rust": object()})
    assert codegraph_defs.regex_fallback_hash_salt("a/b.ps1") == codegraph_defs.REGEX_FALLBACK_HASH_SALT
    assert codegraph_defs.regex_fallback_hash_salt("a/b.rs") == ""
    assert codegraph_defs.regex_fallback_hash_salt("a/b.py") == ""  # ast, no grammar
    assert codegraph_defs.definitions_grammar_for_path("x.tsx") == "tsx"
    assert codegraph_defs.definitions_grammar_for_path("x.svelte") == "typescript"
    assert codegraph_defs.definitions_grammar_for_path("x.proto") == "proto"


class _GateCtx:
    """The two analyzer seams ``run_pure_extractor`` touches."""

    def __init__(self, stored_hash: str | None) -> None:
        self.stored_hash = stored_hash
        self.written = None

    def _get_existing_module(self, path, file_hash):
        return "uuid" if file_hash == self.stored_hash else None

    def write_file_extraction(self, fx):
        self.written = fx
        return {"modules": 1}


def test_installing_a_parser_re_extracts_that_languages_unchanged_files(tmp_path, monkeypatch) -> None:
    """The file gate skips an unchanged file whose stored hash matches. A file
    stored by the regex fallback carries the salted hash, so once the parser
    loads the next walk re-extracts it — and only languages whose grammar
    availability changed are affected."""
    from vco_lib.codegraph_lang._shared import run_pure_extractor
    from vco_lib.codegraph_entities import FileExtraction, ModuleDescriptor
    from datetime import datetime, timezone

    src = tmp_path / "deploy.ps1"
    src.write_text("function A { }\n")
    plain = hashlib.sha256(src.read_text().encode()).hexdigest()

    def extract(content, fp, root, helpers):
        return FileExtraction(module=ModuleDescriptor(
            path="deploy.ps1", language="PowerShell", loc=1, complexity=1.0,
            last_modified=datetime.now(timezone.utc), file_hash=plain, imports=[],
            module_summary=""))

    # 1) parser absent: the module row is stamped with the SALTED hash.
    monkeypatch.setattr(codegraph_grammars, "_LANGUAGES", {"powershell": None})
    ctx = _GateCtx(stored_hash=None)
    run_pure_extractor(ctx, src, tmp_path, extract, {"modules": 0})
    assert ctx.written is not None and ctx.written.module is not None
    salted = ctx.written.module.file_hash
    assert salted != plain
    # 2) still absent: unchanged file -> skipped (no re-extraction churn).
    ctx = _GateCtx(stored_hash=salted)
    run_pure_extractor(ctx, src, tmp_path, extract, {"modules": 0})
    assert ctx.written is None
    # 3) parser installed: the same unchanged file is re-extracted, stamped plain.
    monkeypatch.setattr(codegraph_grammars, "_LANGUAGES", {"powershell": object()})
    ctx = _GateCtx(stored_hash=salted)
    run_pure_extractor(ctx, src, tmp_path, extract, {"modules": 0})
    assert ctx.written is not None and ctx.written.module.file_hash == plain


def test_offer_cli_prints_one_json_object(tmp_path, capsys) -> None:
    proj = tmp_path / "p"
    proj.mkdir()
    assert cp.main(["offer", "--project", str(proj)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {"offers", "broken", "languages", "errors"}
    assert Path(os.environ["VCT_STATE_DIR"], cp.STATE_BASENAME).exists()
