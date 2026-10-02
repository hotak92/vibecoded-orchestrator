# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.100 — the WP-18 review fixes (R18-03, -06, -07, -09, -12, -13, -15).

(R18-01, the CLAUDE.md split, has its own file:
``tests/test_v02100_claude_md_user_section.py``.)
"""
from __future__ import annotations

import contextlib
import dataclasses
import io
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests._materialize_fixtures import mirror_repo_tree  # noqa: E402
from tests.common.launcher_db_fixture import make_launcher_db  # noqa: E402
from vco_lib import boot_service, materialize, project_init, rewire  # noqa: E402
from vco_lib import rendered_root_files as rrf  # noqa: E402
from vco_lib import service_endpoints as se  # noqa: E402
from vco_lib.deferral_report import DeferralReport  # noqa: E402

DEAD = "{{ORCHESTRATOR_ROOT}}/claude_mcp_servers/.venv/bin/python"


def _cids(folder: Path) -> set:
    return {e.condition_id for e in DeferralReport.read(folder).entries}


def _fake_orch(root: Path) -> Path:
    (root / "templates" / "agents" / "free").mkdir(parents=True, exist_ok=True)
    (root / "vct-module.json").write_text("{}\n", encoding="utf-8")
    py = materialize.venv_python_path(root)
    py.parent.mkdir(parents=True, exist_ok=True)
    py.write_text("", encoding="utf-8")
    return root


def _bundle(project: Path, orch: Path, *, update: bool) -> dict:
    with contextlib.redirect_stderr(io.StringIO()):
        return project_init.install_project_bundle(
            project, orchestrator_root=orch, update_mode=update)


def _render(text: str, root: Path, *, escape: str = "none") -> materialize.RenderResult:
    ctx = materialize.LazyContext(materialize.MaterializeContext(root, root))
    return materialize.render(text, ctx, allowed=materialize.GLOBAL_KEYS, escape=escape)


# ══════════════════════════════════════════════════════════════════════
# R18-03 — the COMPOSITE rendered path is checked
# ══════════════════════════════════════════════════════════════════════
class TestCompositePaths:
    def test_the_dead_agent_interpreter_path_is_caught(self, tmp_path):
        """The exact defect that motivated owner rule 2."""
        result = _render(f"command: {DEAD}\n", tmp_path)
        (missing,) = result.missing_paths
        assert missing.name == "ORCHESTRATOR_ROOT"
        assert missing.value == f"{tmp_path}/claude_mcp_servers/.venv/bin/python"
        folder = tmp_path / "proj"
        folder.mkdir()
        materialize.settle_deferrals(folder, {"agent.md": result})
        assert materialize.path_missing_condition_id("agent.md") in _cids(folder)

    def test_an_existing_composite_is_clean(self, tmp_path):
        (tmp_path / "tools" / "vct-secrets").mkdir(parents=True)
        (tmp_path / "tools" / "vct-secrets" / "vct").write_text("", encoding="utf-8")
        assert _render("Run `{{ORCHESTRATOR_ROOT}}/tools/vct-secrets/vct`.", tmp_path).clean

    @pytest.mark.parametrize("line", [
        "{{PROJECT_ROOT}}/integrations/{provider}/client.py",   # pattern
        "{{PROJECT_ROOT}}/integrations/acme/",                  # created later
        "{{PROJECT_ROOT}}/.claude/backups/x",                   # created later
        "see {{ORCHESTRATOR_ROOT}}/<name>.md",                  # prose example
        "{{ORCHESTRATOR_ROOT}}</WorkingDirectory>",             # not a path tail
        "{{ORCHESTRATOR_ROOT}}/",                               # bare root
    ])
    def test_patterns_later_paths_and_non_paths_are_not_checked(self, tmp_path, line):
        assert _render(line, tmp_path).clean, line

    def test_trailing_punctuation_is_not_part_of_the_path(self, tmp_path):
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "A.md").write_text("", encoding="utf-8")
        assert _render("Read {{ORCHESTRATOR_ROOT}}/docs/A.md.", tmp_path).clean
        assert _render("({{ORCHESTRATOR_ROOT}}/docs/A.md), then", tmp_path).clean

    def test_a_missing_root_is_reported_once_as_the_root(self, tmp_path):
        gone = tmp_path / "gone"
        ctx = materialize.LazyContext(materialize.MaterializeContext(gone, tmp_path))
        result = materialize.render(f"x {DEAD}\n", ctx, allowed={"ORCHESTRATOR_ROOT"},
                                    escape="none")
        assert [m.value for m in result.missing_paths] == [str(gone)]

    def test_the_gate_turns_red_on_the_dead_path(self, tmp_path, monkeypatch):
        """Mutation proof through the SHIPPED spec of a real agent, against a
        synthetic install that is a real copy of the tree's layout."""
        from tests.test_template_materialization_complete import (
            _bundle_ops, _make, _violations,
        )

        syn = _make(tmp_path, "Linux")
        op = next(o for o in _bundle_ops(monkeypatch)
                  if isinstance(o.transform, materialize.Transform)
                  and o.dest_rel.endswith("coder.md"))
        mutated = op.source_abs.read_bytes() + f"\nInterpreter: {DEAD}\n".encode()
        data, result = op.transform.render(mutated, materialize.LazyContext(syn.context))
        violations = _violations({op.dest_rel: (data.decode("utf-8"), result)})
        assert any("claude_mcp_servers/.venv/bin/python" in v for v in violations), violations


# ══════════════════════════════════════════════════════════════════════
# R18-15 — a Markdown `---` rule is not YAML frontmatter
# ══════════════════════════════════════════════════════════════════════
class TestFrontmatterDetection:
    @pytest.mark.parametrize("prose", [
        "Note: see {{ORCHESTRATOR_ROOT}}/x for more, it is: odd",
        "A paragraph that mentions {{ORCHESTRATOR_ROOT}}/x: and no key",
    ])
    def test_a_leading_horizontal_rule_leaves_prose_unquoted(self, tmp_path, prose):
        text = f"---\n{prose}\n---\nBody\n"
        result = _render(text, tmp_path, escape="yaml")
        assert prose.replace("{{ORCHESTRATOR_ROOT}}", str(tmp_path)) in result.text
        assert '"' not in result.text

    def test_real_frontmatter_is_still_yaml_escaped(self, tmp_path):
        root = tmp_path / "a: & b"
        root.mkdir()
        text = "---\nname: x\ncommand: {{ORCHESTRATOR_ROOT}}\n---\nBody {{ORCHESTRATOR_ROOT}}\n"
        result = _render(text, root, escape="yaml")
        assert f'command: "{root}"' in result.text
        assert f"Body {root}" in result.text


# ══════════════════════════════════════════════════════════════════════
# R18-06 — the moved-clone heal IS the forward render
# ══════════════════════════════════════════════════════════════════════
REGION_PY = (
    "# VCO-REWIRE-BEGIN: orchestrator-root-resolution\n"
    'ROOT = "{{ORCHESTRATOR_ROOT}}"\n'
    "# VCO-REWIRE-END: orchestrator-root-resolution\n"
    "print(ROOT)\n"
).encode("utf-8")
AGENT = (
    "---\nname: fixture\ncommand: {{VENV_PYTHON}}\n---\n"
    "Run {{VENV_PYTHON}} from {{ORCHESTRATOR_ROOT}}\n"
).encode("utf-8")


class TestMovedCloneHeal:
    def test_a_rewired_py_from_a_windows_shaped_old_root_heals(self, tmp_path):
        project = tmp_path / "proj"
        project.mkdir()
        old_root = Path(r"C:\Users\ann\old vco")  # py-escaped when baked
        old = rewire.rewire_transform(old_root, filename="x.py", project_root=project)(REGION_PY)
        assert b"C:\\\\Users\\\\ann" in old
        target = project / "x.py"
        target.write_bytes(old)
        new_root = tmp_path / "new vco"
        transform = rewire.rewire_transform(new_root, filename="x.py", project_root=project)
        assert project_init._stale_orchestrator_root_heal_match(
            REGION_PY, target, new_root, project, transform)
        target.write_bytes(old.replace(b"print(ROOT)", b"print('mine', ROOT)"))
        assert not project_init._stale_orchestrator_root_heal_match(
            REGION_PY, target, new_root, project, transform)

    def test_an_agent_using_venv_python_and_a_yaml_quoted_root_heals(self, tmp_path):
        project = tmp_path / "proj"
        project.mkdir()
        spec = materialize.RenderSpec(allowed=materialize.GLOBAL_KEYS, escape="yaml")
        old_root = tmp_path / "old: & vco"
        old = materialize.Transform(
            "a.md", spec, materialize.MaterializeContext(old_root, project))(AGENT)
        assert b'command: "' in old, "the old root needed YAML quoting"
        target = project / "a.md"
        target.write_bytes(old)
        new_root = tmp_path / "new"
        transform = materialize.Transform(
            "a.md", spec, materialize.MaterializeContext(new_root, project))
        assert project_init._stale_orchestrator_root_heal_match(
            AGENT, target, new_root, project, transform)

    def test_the_bundle_heals_it_without_a_backup(self, tmp_path):
        """Through the real update path: manifest lost, clone moved."""
        old_orch = _fake_orch(tmp_path / "old: & vco")
        (old_orch / "templates" / "agents" / "free" / "fixture.md").write_bytes(AGENT)
        project = tmp_path / "proj"
        project.mkdir()
        _bundle(project, old_orch, update=False)
        (project / ".claude" / ".vco-manifest.json").unlink()
        new_orch = _fake_orch(tmp_path / "new vco")
        (new_orch / "templates" / "agents" / "free" / "fixture.md").write_bytes(AGENT)
        res = _bundle(project, new_orch, update=True)
        text = (project / ".claude" / "agents" / "fixture.md").read_text(encoding="utf-8")
        assert str(new_orch) in text and str(old_orch) not in text
        assert not (project / ".claude" / "backups").exists(), res.get("actions")

    def test_a_non_materializer_transform_never_heals(self, tmp_path):
        target = tmp_path / "f"
        target.write_bytes(b"x")
        assert not project_init._stale_orchestrator_root_heal_match(
            b"x", target, tmp_path, None, lambda b: b)


# ══════════════════════════════════════════════════════════════════════
# R18-07 — each settle call site, end to end (red if one is deleted)
# ══════════════════════════════════════════════════════════════════════
class TestSettleCallSitesEndToEnd:
    def test_bundle_writes_the_token_rows_it_and_a_clean_update_clears_it(self, tmp_path):
        orch = _fake_orch(tmp_path / "orch")
        tpl = orch / "templates" / "agents" / "free" / "fixture.md"
        tpl.write_text("---\nname: fixture\n---\nUses {{NOT_A_KEY}}.\n", encoding="utf-8")
        project = tmp_path / "proj"
        project.mkdir()
        res = _bundle(project, orch, update=False)
        assert not res["errors"], res["errors"]
        dest = project / ".claude" / "agents" / "fixture.md"
        assert "{{NOT_A_KEY}}" in dest.read_text(encoding="utf-8")
        cid = materialize.unrendered_condition_id(str(Path(".claude") / "agents" / "fixture.md"))
        assert cid in _cids(project)
        assert res["materialize_deferrals"]["emitted"] == [cid]
        tpl.write_text("---\nname: fixture\n---\nUses {{PROJECT_ROOT}}.\n", encoding="utf-8")
        _bundle(project, orch, update=True)
        assert cid not in _cids(project)

    def test_root_table_rows_the_install_root_and_a_clean_render_clears_it(self, tmp_path):
        root = tmp_path / "root"
        mirror_repo_tree(root)
        _fake_orch(root)
        real = (REPO_ROOT / "templates" / "ORCHESTRATOR-CLAUDE.md.template").read_text(
            encoding="utf-8")
        tpl = root / "templates" / "ORCHESTRATOR-CLAUDE.md.template"
        lines = real.splitlines(keepends=True)
        tpl.write_text(lines[0] + "Bogus {{NOT_A_KEY}}\n" + "".join(lines[1:]),
                       encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()):
            (outcome,) = rrf.render_all(root)
        assert outcome.status in ("created", "full_rewrite", "auto_block_updated")
        assert "{{NOT_A_KEY}}" in (root / "CLAUDE.md").read_text(encoding="utf-8")
        cid = materialize.unrendered_condition_id("CLAUDE.md")
        assert cid in _cids(root)
        tpl.write_text(real, encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()):
            rrf.render_all(root)
        assert cid not in _cids(root)

    def _unit(self, tmp_path, monkeypatch):
        monkeypatch.setattr(boot_service.shutil, "which", lambda _n: None)
        install = tmp_path / "vco"
        (install / "infrastructure").mkdir(parents=True)
        (install / "scripts").mkdir(parents=True)
        (install / "scripts" / "launch-claude-mcp-stack.sh").write_text("", encoding="utf-8")
        spec = dataclasses.replace(
            boot_service.container_stack_spec(install, install / "infrastructure",
                                              os_key="Linux"),
            deferral_folder=install)
        return install, spec

    def test_boot_unit_rows_the_deferral_folder_and_a_clean_render_clears_it(
            self, tmp_path, monkeypatch):
        install, spec = self._unit(tmp_path, monkeypatch)
        home = tmp_path / "home"
        tpl = boot_service.read_template(REPO_ROOT, spec.template_linux)
        assert tpl is not None
        with contextlib.redirect_stderr(io.StringIO()):
            boot_service.register_linux(spec, tpl + "\n# {{NOT_A_KEY}}\n", home=home)
        unit = boot_service.systemd_unit_path(spec, home)
        assert "{{NOT_A_KEY}}" in unit.read_text(encoding="utf-8")
        cid = materialize.unrendered_condition_id(boot_service.unit_label(spec, "Linux"))
        assert cid in _cids(install)
        with contextlib.redirect_stderr(io.StringIO()):
            boot_service.register_linux(spec, tpl, home=home)
        assert cid not in _cids(install)

    # R18-09: an unregistered unit's rows are resolved.
    def test_unregistering_a_unit_resolves_its_rows(self, tmp_path, monkeypatch):
        install, spec = self._unit(tmp_path, monkeypatch)
        home = tmp_path / "home"
        tpl = boot_service.read_template(REPO_ROOT, spec.template_linux)
        with contextlib.redirect_stderr(io.StringIO()):
            boot_service.register_linux(spec, tpl + "\n# {{NOT_A_KEY}}\n", home=home)
        cid = materialize.unrendered_condition_id(boot_service.unit_label(spec, "Linux"))
        assert cid in _cids(install)
        boot_service.unregister(spec, home=home, system="Linux", runner=lambda *_a, **_k: 0)
        assert cid not in _cids(install)


# ══════════════════════════════════════════════════════════════════════
# R18-09 — bundle rows nothing would otherwise clear
# ══════════════════════════════════════════════════════════════════════
class TestOrphanedBundleRows:
    def _install_with_bad_agent(self, tmp_path):
        orch = _fake_orch(tmp_path / "orch")
        tpl = orch / "templates" / "agents" / "free" / "fixture.md"
        tpl.write_text("---\nname: fixture\n---\nUses {{NOT_A_KEY}}.\n", encoding="utf-8")
        project = tmp_path / "proj"
        project.mkdir()
        _bundle(project, orch, update=False)
        cid = materialize.unrendered_condition_id(str(Path(".claude") / "agents" / "fixture.md"))
        assert cid in _cids(project)
        return orch, project, tpl, cid

    def test_a_template_removed_by_a_later_release_resolves_its_row(self, tmp_path):
        orch, project, tpl, cid = self._install_with_bad_agent(tmp_path)
        tpl.unlink()
        _bundle(project, orch, update=True)
        assert cid not in _cids(project)

    def test_a_disabled_agent_creates_no_row_and_resolves_its_old_one(self, tmp_path):
        orch, project, _tpl, cid = self._install_with_bad_agent(tmp_path)
        disabled = project / ".claude" / "agents.disabled"
        disabled.mkdir(parents=True)
        (project / ".claude" / "agents" / "fixture.md").rename(disabled / "fixture.md")
        res = _bundle(project, orch, update=True)
        assert cid not in _cids(project)
        assert cid not in (res.get("materialize_deferrals") or {}).get("emitted", [])

    def test_the_sweep_leaves_other_surfaces_rows_alone(self, tmp_path):
        """The root ledger is shared by the bundle, the root table and the
        boot units: the bundle's sweep resolves ONLY bundle rows."""
        folder = tmp_path / "root"
        folder.mkdir()
        bad = _render("{{NOT_A_KEY}}", tmp_path)
        materialize.settle_deferrals(folder, {"CLAUDE.md": bad}, surface="root-file")
        materialize.settle_deferrals(folder, {"systemd:x.service": bad}, surface="boot-unit")
        materialize.settle_deferrals(folder, {"gone.md": bad}, surface="bundle")
        materialize.settle_deferrals(folder, {}, surface="bundle", shipped={"kept.md"})
        cids = _cids(folder)
        assert materialize.unrendered_condition_id("gone.md") not in cids
        assert materialize.unrendered_condition_id("CLAUDE.md") in cids
        assert materialize.unrendered_condition_id("systemd:x.service") in cids


# ══════════════════════════════════════════════════════════════════════
# R18-12 — an endpoint move re-renders the root file that bakes it
# ══════════════════════════════════════════════════════════════════════
class TestEndpointMoveRerendersRoot:
    def _root(self, tmp_path) -> Path:
        root = tmp_path / "root"
        mirror_repo_tree(root)
        _fake_orch(root)
        (root / "templates" / "ORCHESTRATOR-CLAUDE.md.template").write_bytes(
            (REPO_ROOT / "templates" / "ORCHESTRATOR-CLAUDE.md.template").read_bytes())
        return root

    def _move(self, root: Path, db: Path) -> se.ApplyChangeReport:
        se.write_rows([se.EndpointRow(service="weaviate", mode="vco_managed", port=18081,
                                      grpc_port=60052, source="user_cli")], db_path=db)
        return se.apply_change(["weaviate"], orchestrator_root=root, db_path=db,
                               write_infra_env=lambda *_a: None, reproject=lambda _d: None,
                               register_mcps=lambda _r: True, out=lambda _l: None)

    def test_the_auto_block_follows_the_row(self, tmp_path):
        root = self._root(tmp_path)
        db = make_launcher_db(tmp_path / "launcher.db")
        with contextlib.redirect_stderr(io.StringIO()):
            rrf.render_all(root, db_path=db)  # the install's render: default port
        assert "http://localhost:8081" in (root / "CLAUDE.md").read_text(encoding="utf-8")
        (root / "CLAUDE.md").write_text(
            "mine\n" + (root / "CLAUDE.md").read_text(encoding="utf-8"), encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()):
            report = self._move(root, db)
        assert report.steps["root_files"] == "ok", report.errors
        text = (root / "CLAUDE.md").read_text(encoding="utf-8")
        assert "http://localhost:18081" in text and "http://localhost:8081/" not in text
        assert text.startswith("mine\n")

    def test_a_file_no_install_rendered_is_never_created_or_rewritten(self, tmp_path):
        root = self._root(tmp_path)
        db = make_launcher_db(tmp_path / "launcher.db")
        with contextlib.redirect_stderr(io.StringIO()):
            report = self._move(root, db)
        assert report.steps["root_files"] == "ok"
        assert not (root / "CLAUDE.md").exists()
        (root / "CLAUDE.md").write_text("# a checkout's tracked file\n", encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()):
            self._move(root, db)
        assert (root / "CLAUDE.md").read_text(encoding="utf-8") == "# a checkout's tracked file\n"


# ══════════════════════════════════════════════════════════════════════
# R18-13 — `render_template` is the one boot-unit entry point
# ══════════════════════════════════════════════════════════════════════
def test_every_unit_render_goes_through_render_template(tmp_path, monkeypatch):
    monkeypatch.setattr(boot_service.shutil, "which", lambda _n: None)
    calls: list = []
    real = boot_service.render_template

    def _spy(*a, **k):
        calls.append(k.get("label"))
        return real(*a, **k)

    monkeypatch.setattr(boot_service, "render_template", _spy)
    install = tmp_path / "vco"
    (install / "infrastructure").mkdir(parents=True)
    (install / "scripts").mkdir(parents=True)
    for name in ("launch-claude-mcp-stack.sh", "launch-claude-mcp-stack.ps1"):
        (install / "scripts" / name).write_text("", encoding="utf-8")
    home = tmp_path / "home"
    for os_key, register in (("Linux", boot_service.register_linux),
                             ("Darwin", boot_service.register_macos)):
        spec = boot_service.container_stack_spec(install, install / "infrastructure",
                                                 os_key=os_key)
        tpl = boot_service.read_template(
            REPO_ROOT, spec.template_linux if os_key == "Linux" else spec.template_macos)
        register(spec, tpl, home=home)
    spec = dataclasses.replace(
        boot_service.container_stack_spec(install, install / "infrastructure",
                                          os_key="Windows"),
        windows_task_xml_path=tmp_path / "task.xml")
    boot_service.register_windows(spec, boot_service.read_template(REPO_ROOT,
                                                                   spec.template_windows))
    assert calls == [boot_service.unit_label(spec, o) for o in ("Linux", "Darwin", "Windows")]


def test_the_hub_port_line_says_it_is_a_snapshot_and_where_the_live_value_is():
    """R18-12 (wording half): `{{HUB_PORT}}` is resolved at render time, and a
    hub restarted on a fallback port does not re-render the file — so the line
    must say so and point at the live value and the command that prints it."""
    text = (REPO_ROOT / "templates" / "ORCHESTRATOR-CLAUDE.md.template").read_text(
        encoding="utf-8")
    (line,) = [ln for ln in text.splitlines() if "{{HUB_PORT}}" in ln]
    assert "last rendered" in line and "hub.port" in line
    assert "vct_project_config.sh hub-port" in line
    assert (REPO_ROOT / "templates" / "scripts" / "vct_project_config.sh").is_file()
    assert "snapshot" in materialize.REGISTRY["HUB_PORT"].doc
