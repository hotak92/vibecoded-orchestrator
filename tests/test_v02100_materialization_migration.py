# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.100 WP-18 — an UPDATE replaces what the old, bugged renderers wrote.

Owner requirement (2026-09-30): after this release a user who updates gets the
newly, correctly materialized files REPLACING the ones the old renderers
produced (the dead ``<root>/claude_mcp_servers/.venv/bin/python``, a stale
project CLAUDE.md managed region, an unescaped plist, …) — and a file whose
bytes are exactly the OLD renderer's output is treated as UNTOUCHED
(overwritten silently), never as user-modified. A file the user DID edit keeps
today's rule: backup under ``.claude/backups/bundle-adoptions/<ts>/``, then the
new render.

One class per test group. Each builds the old install's state the way the old
code left it (the bugged bytes AND the manifest/sidecar the old code recorded),
runs the ordinary update path, and asserts the new render landed with no false
"user-modified" backup or deferral — plus a user-edited variant that IS backed
up.
"""
from __future__ import annotations

import contextlib
import io
import json
import plistlib
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import boot_service, materialize, project_init  # noqa: E402
from vco_lib import rendered_root_files as rrf  # noqa: E402
from vco_lib.deferral_report import DeferralReport  # noqa: E402
from vco_lib.hashing import sha256_file  # noqa: E402
from tests._materialize_fixtures import mirror_repo_tree  # noqa: E402

AGENT_REL = str(Path(".claude") / "agents" / "coder-fixture.md")

# The pre-v0.2.100 agent template shape (survey gap b.1) and the new one.
OLD_AGENT = """---
name: coder-fixture
description: fixture
mcpServers:
  orchestrator-tools:
    command: {{ORCHESTRATOR_ROOT}}/claude_mcp_servers/.venv/bin/python
---

# Body mentions {{ORCHESTRATOR_ROOT}}
"""
NEW_AGENT = """---
name: coder-fixture
description: fixture
interpreter: {{VENV_PYTHON}}
---

# Body mentions {{ORCHESTRATOR_ROOT}}
"""


def _old_apply_subs(text: str, orch: Path, project: Path) -> str:
    """The pre-v0.2.100 R1 renderer, verbatim in behaviour: a raw
    ``str.replace`` loop, no escaping, unknown tokens passed through."""
    subs = {
        "{{ORCHESTRATOR_ROOT}}": str(orch),
        "{{PROJECT_ROOT}}": str(project),
        "{{PROJECTS_ROOT}}": str(orch.parent),
        "{{HOME}}": str(Path.home()),
        "{{VCT_ORCHESTRATOR_ROOT}}": "${VCT_ORCHESTRATOR_ROOT}",
    }
    for k, v in subs.items():
        text = text.replace(k, v)
    return text


def _fake_orch(root: Path) -> None:
    (root / "vct-module.json").write_text("{}\n", encoding="utf-8")
    (root / "templates" / "agents" / "free").mkdir(parents=True, exist_ok=True)
    py = materialize.venv_python_path(root)
    py.parent.mkdir(parents=True, exist_ok=True)
    py.write_text("", encoding="utf-8")


def _manifest_path(project: Path) -> Path:
    return project / ".claude" / ".vco-manifest.json"


def _record_as_old_install(project: Path, rel: str) -> None:
    """The old install recorded the POST-transform hash of what it wrote."""
    m = json.loads(_manifest_path(project).read_text(encoding="utf-8"))
    m["files"][rel]["sha256"] = sha256_file(project / rel)
    _manifest_path(project).write_text(json.dumps(m, indent=2), encoding="utf-8")


def _backups(project: Path) -> list:
    root = project / ".claude" / "backups" / "bundle-adoptions"
    return sorted(p for p in root.rglob("*") if p.is_file()) if root.exists() else []


def _cids(project: Path) -> set:
    return {e.condition_id for e in DeferralReport.read(project).entries}


def _bundle(project: Path, orch: Path, *, update: bool) -> dict:
    with contextlib.redirect_stderr(io.StringIO()):
        return project_init.install_project_bundle(
            project, orchestrator_root=orch, update_mode=update)


# ══════════════════════════════════════════════════════════════════════
# Class R1 — agents / skills (manifest-driven)
# ══════════════════════════════════════════════════════════════════════
class TestAgentsMigrate:
    @pytest.fixture
    def old_install(self, tmp_path):
        # A clone path the old renderer baked raw into YAML: `&` and `: ` —
        # the new renderer quotes the scalar, so the bytes differ.
        orch = tmp_path / "vco: & clone"
        orch.mkdir()
        _fake_orch(orch)
        tpl = orch / "templates" / "agents" / "free" / "coder-fixture.md"
        tpl.write_text(OLD_AGENT, encoding="utf-8")
        project = tmp_path / "proj"
        project.mkdir()
        _bundle(project, orch, update=False)
        # Reproduce the OLD renderer's bytes + the manifest it recorded.
        (project / AGENT_REL).write_text(
            _old_apply_subs(OLD_AGENT, orch, project), encoding="utf-8")
        _record_as_old_install(project, AGENT_REL)
        tpl.write_text(NEW_AGENT, encoding="utf-8")  # the release's template
        return orch, project

    def test_untouched_old_render_is_overwritten_silently(self, old_install):
        orch, project = old_install
        res = _bundle(project, orch, update=True)
        assert AGENT_REL in res["actions"]["overwrite"], res["actions"]
        text = (project / AGENT_REL).read_text(encoding="utf-8")
        assert str(materialize.venv_python_path(orch)) in text
        assert "claude_mcp_servers/.venv" not in text
        assert _backups(project) == []
        assert "bundle_user_modified_preserved" not in _cids(project)
        assert not any(c.startswith(materialize.UNRENDERED_PREFIX) for c in _cids(project))

    def test_user_edited_old_render_is_backed_up_then_replaced(self, old_install):
        orch, project = old_install
        edited = (project / AGENT_REL).read_text(encoding="utf-8") + "\nMY NOTE\n"
        (project / AGENT_REL).write_text(edited, encoding="utf-8")
        res = _bundle(project, orch, update=True)
        assert AGENT_REL in res["actions"]["adopt"], res["actions"]
        backups = _backups(project)
        assert len(backups) == 1 and "MY NOTE" in backups[0].read_text(encoding="utf-8")
        assert "MY NOTE" not in (project / AGENT_REL).read_text(encoding="utf-8")


# ══════════════════════════════════════════════════════════════════════
# Class R3 — `VCO-REWIRE` scripts (manifest-driven, moved clone)
# ══════════════════════════════════════════════════════════════════════
class TestRewiredScriptsMigrate:
    def test_old_root_render_is_overwritten_silently(self, tmp_path):
        from vco_lib import rewire

        name = "get_node_info.py"
        rel = str(Path(".claude") / "scripts" / name)
        src = (REPO_ROOT / "templates" / "scripts" / name).read_bytes()
        old, new = tmp_path / "old" / "vco", tmp_path / "new" / "vco"
        for root in (old, new):
            root.mkdir(parents=True)
            _fake_orch(root)
            (root / "templates" / "scripts").mkdir(parents=True)
            (root / "templates" / "scripts" / name).write_bytes(src)
        project = tmp_path / "proj"
        project.mkdir()
        _bundle(project, new, update=False)
        # The OLD renderer's bytes (pre-v0.2.100 rewire, old root) + its hash.
        (project / rel).write_bytes(rewire.rewire_bytes(src, old, filename=name))
        _record_as_old_install(project, rel)
        res = _bundle(project, new, update=True)
        assert rel in res["actions"]["overwrite"]
        text = (project / rel).read_text(encoding="utf-8")
        assert str(new) in text and str(old) not in text
        assert _backups(project) == []


# ══════════════════════════════════════════════════════════════════════
# Class R2 — project CLAUDE.md managed region (sidecar is the old render)
# ══════════════════════════════════════════════════════════════════════
OLD_CLAUDE_TPL = "# {{PROJECT_NAME}}\n\nOld clone: `{{ORCHESTRATOR_ROOT}}/claude_mcp_servers/.venv`\n"
NEW_CLAUDE_TPL = "# {{PROJECT_NAME}}\n\nRun `{{VENV_PYTHON}} -m vco_lib.project_init ...`\n"
USER_TAIL = "\n## My own notes\nkeep me\n"


class TestProjectClaudeMdMigrates:
    @pytest.fixture
    def old_install(self, tmp_path):
        orch = tmp_path / "orch"
        orch.mkdir()
        _fake_orch(orch)
        tpl = orch / "templates" / "CLAUDE.md.template"
        tpl.write_text(OLD_CLAUDE_TPL, encoding="utf-8")
        project = tmp_path / "proj"
        project.mkdir()
        _bundle(project, orch, update=False)  # creates the marked live file
        live = project / "CLAUDE.md"
        live.write_text(live.read_text(encoding="utf-8") + USER_TAIL, encoding="utf-8")
        # The old update path wrote the sidecar = the old render (and left the
        # live managed body alone — the defect).
        _bundle(project, orch, update=True)
        sidecar = project / ".claude" / "context" / "templates" / "CLAUDE.md.reference.md"
        assert sidecar.is_file()
        tpl.write_text(NEW_CLAUDE_TPL, encoding="utf-8")
        return orch, project

    def test_untouched_managed_body_is_rerendered_silently(self, old_install):
        orch, project = old_install
        res = _bundle(project, orch, update=True)
        text = (project / "CLAUDE.md").read_text(encoding="utf-8")
        assert str(materialize.venv_python_path(orch)) in text
        assert "claude_mcp_servers/.venv" not in text
        assert text.endswith(USER_TAIL), "content outside the markers is the user's"
        assert res["templates"]["managed_rerendered"] == ["CLAUDE.md"]
        assert res["templates"]["managed_backups"] == []
        assert "CLAUDE.md" not in res["templates"]["diverged"]
        assert _backups(project) == []
        assert "template_review_pending" not in _cids(project)

    def test_user_edited_managed_body_is_backed_up_then_rerendered(self, old_install):
        orch, project = old_install
        live = project / "CLAUDE.md"
        live.write_text(live.read_text(encoding="utf-8").replace(
            "Old clone:", "MY EDIT Old clone:"), encoding="utf-8")
        res = _bundle(project, orch, update=True)
        (backup,) = _backups(project)
        assert "MY EDIT" in backup.read_text(encoding="utf-8")
        assert res["templates"]["managed_backups"]
        text = live.read_text(encoding="utf-8")
        assert "MY EDIT" not in text and text.endswith(USER_TAIL)

    def test_one_project_name_source_for_both_callers(self, old_install):
        """Bundle update and the launcher's re-render agree (survey gap d.2)."""
        orch, project = old_install
        _bundle(project, orch, update=True)
        via_bundle = (project / "CLAUDE.md").read_text(encoding="utf-8")
        project_init.render_claude_md(project, orchestrator_root=orch,
                                      project_name=project.name)
        assert (project / "CLAUDE.md").read_text(encoding="utf-8") == via_bundle

    def test_a_claude_md_without_markers_is_left_alone(self, tmp_path):
        orch = tmp_path / "orch"
        orch.mkdir()
        _fake_orch(orch)
        (orch / "templates" / "CLAUDE.md.template").write_text(
            NEW_CLAUDE_TPL, encoding="utf-8")
        project = tmp_path / "proj"
        project.mkdir()
        mine = "# My project\nnothing of VCO's\n"
        (project / "CLAUDE.md").write_text(mine, encoding="utf-8")
        res = _bundle(project, orch, update=True)
        from vco_lib.deferral_report import strip_vco_owned_regions

        # Only VCO's own reminder block may appear (a first update records
        # the template review); the user's file body is untouched.
        after = strip_vco_owned_regions((project / "CLAUDE.md").read_text(encoding="utf-8"))
        assert after.strip("\n") == mine.strip("\n")
        assert res["templates"]["managed_rerendered"] == []


# ══════════════════════════════════════════════════════════════════════
# Class R5 — orchestrator-root CLAUDE.md AUTO block
# ══════════════════════════════════════════════════════════════════════
class TestRootClaudeMdMigrates:
    def test_old_auto_block_is_rerendered_and_user_text_kept(self, tmp_path):
        root = tmp_path / "root"
        mirror_repo_tree(root)  # composite paths exist, as in a real clone (R18-03)
        (root / "templates").mkdir(parents=True, exist_ok=True)
        _fake_orch(root)
        (root / "templates" / "ORCHESTRATOR-CLAUDE.md.template").write_bytes(
            (REPO_ROOT / "templates" / "ORCHESTRATOR-CLAUDE.md.template").read_bytes())
        old_block = ("<!-- BEGIN: AUTO (old) -->\ncurl -s http://localhost:8081/v1/"
                     ".well-known/ready\n`python -m vco_lib.project_init install-bundle`\n"
                     "<!-- END: AUTO -->")
        (root / "CLAUDE.md").write_text("mine above\n" + old_block + "\nmine below\n",
                                        encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()):
            (outcome,) = rrf.render_all(root)
        assert outcome.status == "auto_block_updated" and not outcome.is_failure
        text = (root / "CLAUDE.md").read_text(encoding="utf-8")
        assert text.startswith("mine above\n") and text.endswith("mine below\n")
        assert str(materialize.venv_python_path(root)) in text
        assert materialize.PLACEHOLDER_RE.findall(text) == []
        assert "`python -m vco_lib.project_init install-bundle`" not in text
        assert not any(c.startswith("template_") for c in _cids(root))


# ══════════════════════════════════════════════════════════════════════
# Class R6 — boot units (re-rendered every run; `.bak-<ts>` is today's rule)
# ══════════════════════════════════════════════════════════════════════
class TestBootUnitsMigrate:
    def test_unescaped_old_plist_is_replaced_by_a_valid_one(self, tmp_path, monkeypatch):
        monkeypatch.setattr(boot_service.shutil, "which", lambda _n: None)
        install = tmp_path / "A & B" / "vco"
        (install / "infrastructure").mkdir(parents=True)
        (install / "scripts").mkdir(parents=True)
        (install / "scripts" / "launch-claude-mcp-stack.sh").write_text("", encoding="utf-8")
        home = tmp_path / "home"
        spec = boot_service.replace(
            boot_service.container_stack_spec(
                install, install / "infrastructure", os_key="Darwin"),
            deferral_folder=None)
        tpl = boot_service.read_template(REPO_ROOT, spec.template_macos)
        # The OLD render: raw values in element content (the survey's gap f).
        old = tpl
        for k, v in {"INSTALLED_AT_PATH": "x", "LABEL": spec.plist_label,
                     "LOG_FILE": str(home / "l.log"), "BOOT_LOG_FILE": str(home / "b.log"),
                     "WORKING_DIR": str(install / "infrastructure"),
                     "WRAPPER_SCRIPT": str(install / "scripts" / "launch-claude-mcp-stack.sh"),
                     }.items():
            old = old.replace("{{" + k + "}}", v)
        target = boot_service.launchd_plist_path(spec, home)
        target.parent.mkdir(parents=True)
        target.write_text(old, encoding="utf-8")
        with pytest.raises(Exception):
            plistlib.loads(old.encode("utf-8"))  # the bugged bytes do not parse
        boot_service.register_macos(spec, tpl, home=home)
        new = target.read_text(encoding="utf-8")
        parsed = plistlib.loads(new.encode("utf-8"))
        assert parsed["WorkingDirectory"] == str(install / "infrastructure")
        assert "&amp;" in new and "&amp;amp;" not in new
