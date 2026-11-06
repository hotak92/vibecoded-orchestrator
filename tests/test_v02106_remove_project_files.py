# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.106 "Remove project": the unregister removes what is VCO's from the
project folder and its settings — and keeps everything that is not.

Every delete is tested both ways (act + leave-alone): a shipped file untouched
since VCO wrote it goes, the same file changed by the user stays; a VCO hook
registration goes, a user's command in the same group stays; VCO's
permissions list goes, a user's own list stays; VCO's prose keys go, a user's
value stays. The collection drop covers THIS project's bound classes and keeps
another project's.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.common.child_env import child_env
from vco_lib import git_exclude, unregister_collections, unregister_files
from vco_lib.hashing import sha256_bytes
from vco_lib.settings_merge import strip_shipped_hooks

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = json.loads((REPO_ROOT / "templates" / "settings.json.linux.template")
                      .read_text(encoding="utf-8"))


def _cmd(script: str) -> str:
    """The exact spelling VCO ships for ``script`` (read off the template)."""
    for entries in TEMPLATE["hooks"].values():
        for entry in entries:
            for h in entry["hooks"]:
                if f"/.claude/hooks/{script}" in h["command"]:
                    return h["command"]
    raise AssertionError(script)


def _ship(folder: Path, files: dict[str, bytes]) -> dict:
    """Write ``files`` and a manifest recording each as shipped."""
    manifest = {"schema_version": 2, "files": {}, "preserved_files": {}, "packs": {}}
    for rel, data in files.items():
        p = folder / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        manifest["files"][rel] = {"sha256": sha256_bytes(data), "source": f"templates/{rel}"}
    (folder / ".claude" / ".vco-manifest.json").write_text(json.dumps(manifest))
    return manifest


def _purge(folder: Path) -> dict:
    return unregister_files.purge_project_files(folder, orchestrator_root=REPO_ROOT)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    folder = tmp_path / "my-project"
    (folder / ".claude").mkdir(parents=True)
    return folder


# ── shipped files: by manifest hash ──────────────────────────────────────

def test_untouched_shipped_files_go_and_changed_ones_stay(project: Path):
    _ship(project, {
        ".claude/agents/code-reviewer.md": b"shipped agent\n",
        ".claude/agents/planner.md": b"shipped planner\n",
        ".claude/hooks/pre-tool-use.sh": b"#!/bin/bash\n",
        ".claude/hooks/_lib/helper.py": b"x = 1\n",
        ".claude/scripts/kg-search": b"#!/bin/bash\n",
        "knowledge/concepts/shipped-node.md": b"# node\n",
    })
    (project / ".claude/agents/planner.md").write_text("my edited planner\n")
    (project / ".claude/agents/my-own-agent.md").write_text("mine\n")
    (project / ".claude/scripts/my-tool.sh").write_text("echo mine\n")
    pyc = project / ".claude/hooks/_lib/__pycache__/helper.cpython-312.pyc"
    pyc.parent.mkdir(parents=True)
    pyc.write_bytes(b"\0")

    out = _purge(project)

    assert not (project / ".claude/agents/code-reviewer.md").exists()
    assert not (project / ".claude/hooks").exists(), "emptied VCO dirs are pruned"
    assert not (project / ".claude/scripts/kg-search").exists()
    assert (project / ".claude/agents/planner.md").read_text() == "my edited planner\n"
    assert (project / ".claude/agents/my-own-agent.md").exists()
    assert (project / ".claude/scripts/my-tool.sh").exists()
    assert (project / "knowledge/concepts/shipped-node.md").exists(), "knowledge is the user's"
    assert not (project / ".claude/.vco-manifest.json").exists()
    assert out["kept_modified"] == [".claude/agents/planner.md"]
    assert ".claude/agents/code-reviewer.md" in out["removed"]
    assert ".claude/agents/my-own-agent.md" not in out["removed"] + out["kept_modified"]
    assert out["errors"] == []


def test_a_launcher_disabled_shipped_agent_goes_from_its_disabled_place(project: Path):
    _ship(project, {".claude/agents/code-reviewer.md": b"shipped\n"})
    disabled = project / ".claude/agents.disabled/code-reviewer.md"
    disabled.parent.mkdir(parents=True)
    (project / ".claude/agents/code-reviewer.md").rename(disabled)
    edited = project / ".claude/agents.disabled/other.md"
    manifest = json.loads((project / ".claude/.vco-manifest.json").read_text())
    manifest["files"][".claude/agents/other.md"] = {"sha256": sha256_bytes(b"shipped other\n")}
    (project / ".claude/.vco-manifest.json").write_text(json.dumps(manifest))
    edited.write_text("edited while disabled\n")

    out = _purge(project)

    assert not disabled.exists()
    assert edited.exists() and out["kept_modified"] == [".claude/agents.disabled/other.md"]


def test_without_a_manifest_nothing_is_proven_so_nothing_shipped_goes(project: Path):
    (project / ".claude/hooks").mkdir()
    (project / ".claude/hooks/pre-tool-use.sh").write_text("#!/bin/bash\n")
    out = _purge(project)
    assert (project / ".claude/hooks/pre-tool-use.sh").exists()
    assert any("missing, so VCO cannot tell" in k for k in out["kept"])


def test_the_orchestrator_clone_is_never_purged(tmp_path: Path):
    clone = tmp_path / "clone"
    (clone / "vco_lib").mkdir(parents=True)
    (clone / ".claude").mkdir()
    _ship(clone, {".claude/hooks/pre-tool-use.sh": b"#!/bin/bash\n"})
    out = _purge(clone)
    assert (clone / ".claude/hooks/pre-tool-use.sh").exists()
    assert out["removed"] == [] and "orchestrator clone" in out["errors"][0]


# ── .claude/settings.json ────────────────────────────────────────────────

def test_vco_hook_registrations_go_and_the_users_stay(project: Path):
    vco = _cmd("pre-tool-use.sh")
    wrapped = "my-backup.sh && " + _cmd("ensure-containers.sh")
    settings = {
        "hooks": {
            "PreToolUse": [
                {"matcher": "*", "hooks": [{"type": "command", "command": vco, "timeout": 5},
                                           {"type": "command", "command": "bash mine.sh"}]},
                {"matcher": "Bash", "hooks": [{"type": "command", "command": vco}]},
            ],
            "SessionStart": [{"matcher": "startup", "hooks": [{"type": "command", "command": wrapped}]}],
            "Stop": [{"hooks": [{"type": "command", "command": _cmd("notify-stop.sh")}]}],
        },
    }
    (project / ".claude/settings.json").write_text(json.dumps(settings))
    out = _purge(project)
    after = json.loads((project / ".claude/settings.json").read_text())
    assert after["hooks"] == {
        "PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": "bash mine.sh"}]}],
        "SessionStart": [{"matcher": "startup", "hooks": [{"type": "command", "command": wrapped}]}],
    }
    assert any(s.startswith("hooks: 3 registration") for s in out["settings_removed"])


def test_the_registration_of_a_hook_script_the_user_changed_stays(project: Path):
    _ship(project, {".claude/hooks/notify-stop.sh": b"#!/bin/bash\n"})
    (project / ".claude/hooks/notify-stop.sh").write_text("#!/bin/bash\necho mine\n")
    stop = {"Stop": [{"hooks": [{"type": "command", "command": _cmd("notify-stop.sh")}]}]}
    (project / ".claude/settings.json").write_text(json.dumps({"hooks": stop}))
    _purge(project)
    assert json.loads((project / ".claude/settings.json").read_text())["hooks"] == stop


def test_vcos_permission_list_goes_and_a_users_own_list_stays(project: Path):
    allow = TEMPLATE["permissions"]["allow"] + ["Bash(make *)"]
    (project / ".claude/settings.json").write_text(json.dumps(
        {"permissions": {"allow": allow, "deny": ["Read(./private/**)"]}}))
    out = _purge(project)
    perms = json.loads((project / ".claude/settings.json").read_text())["permissions"]
    assert perms == {"allow": ["Bash(make *)"], "deny": ["Read(./private/**)"]}
    assert any("permissions.deny" in k for k in out["kept"])


def test_vco_prose_keys_go_user_values_stay_and_an_emptied_file_goes(project: Path):
    path = project / ".claude/settings.json"
    path.write_text(json.dumps({
        "_comment": TEMPLATE["_comment"], "_template_origin": TEMPLATE["_template_origin"],
        "idleCompaction": True, "$schema": TEMPLATE["$schema"], "env": {},
    }))
    out = _purge(project)
    assert json.loads(path.read_text()) == {"idleCompaction": True, "$schema": TEMPLATE["$schema"]}
    assert any("$schema" in k for k in out["kept"])

    path.write_text(json.dumps({"_comment": "my own note", "_env_comment": TEMPLATE["_env_comment"]}))
    _purge(project)
    assert json.loads(path.read_text()) == {"_comment": "my own note"}

    path.write_text(json.dumps({"_comment": TEMPLATE["_comment"],
                                "hooks": {"Stop": [{"hooks": [{"type": "command",
                                                               "command": _cmd("notify-stop.sh")}]}]}}))
    out = _purge(project)
    assert not path.exists() and ".claude/settings.json" in out["removed"]


def test_jsonc_comments_survive_the_strip(project: Path):
    path = project / ".claude/settings.json"
    path.write_text('{\n  // my note\n  "_comment": %s,\n  "model": "x",\n}\n'
                    % json.dumps(TEMPLATE["_comment"]))
    _purge(project)
    text = path.read_text()
    assert "// my note" in text and '"model": "x"' in text and "_comment" not in text


# ── VCO's runtime files ──────────────────────────────────────────────────

def test_vco_runtime_files_go_and_user_bytes_stay(project: Path):
    c = project / ".claude"
    for d in ("state/scratch/lane", "logs", "backups/bundle-adoptions/ts", "context/templates"):
        (c / d).mkdir(parents=True)
    (c / "state/scratch/lane/x").write_text("x")
    (c / "logs/run.log").write_text("log")
    (c / "backups/bundle-adoptions/ts/agent.md").write_text("your old bytes")
    (c / "context/UPDATE_DEFERRED.md").write_text("ledger")
    (c / "context/notes.md").write_text("my notes")
    (c / "context/templates/CLAUDE.md.reference.md").write_text("ref")
    (c / "env").write_text("\n\n")
    (project / ".env.vco.reference").write_text("KEY=\n")
    (project / ".env").write_text("MINE=1\n")

    out = _purge(project)

    for gone in ("state", "logs", "context/UPDATE_DEFERRED.md", "context/templates", "env"):
        assert not (c / gone).exists(), gone
    assert not (project / ".env.vco.reference").exists()
    assert (c / "backups/bundle-adoptions/ts/agent.md").exists()
    assert (c / "context/notes.md").exists() and (project / ".env").exists()
    assert any(k.startswith(".claude/backups/") for k in out["kept"])


def test_a_claude_env_with_user_lines_stays(project: Path):
    (project / ".claude/env").write_text("export MINE=1\n")
    _purge(project)
    assert (project / ".claude/env").read_text() == "export MINE=1\n"


def test_settings_local_goes_only_when_nothing_of_the_users_is_left(project: Path):
    local = project / ".claude/settings.local.json"
    local.write_text(json.dumps({"env": {}}))
    _purge(project)
    assert not local.exists() and not local.parent.exists(), "an emptied .claude/ is pruned"
    local.parent.mkdir()
    local.write_text(json.dumps({"env": {}, "permissions": {"allow": ["Bash(ls)"]}}))
    _purge(project)
    assert local.exists()


def test_vcos_git_exclude_blocks_go_and_the_users_lines_stay(project: Path):
    info = project / ".git/info"
    info.mkdir(parents=True)
    (info / "exclude").write_text(
        "# git ls-files --others --exclude-from=.git/info/exclude\n*.local\n")
    git_exclude.append_git_info_exclude(project, ["/.claude/hooks/x.sh", "/.claude/state/"])
    with (info / "exclude").open("a") as fh:
        fh.write("my-secret-dir/\n")
    out = _purge(project)
    assert (info / "exclude").read_text() == (
        "# git ls-files --others --exclude-from=.git/info/exclude\n*.local\nmy-secret-dir/\n")
    assert any(s.startswith(".git/info/exclude") for s in out["settings_removed"])


def test_the_cli_prints_one_ok_reply(project: Path):
    _ship(project, {".claude/agents/a.md": b"a\n"})
    proc = subprocess.run(
        [sys.executable, "-m", "vco_lib.unregister_files", "purge", "--project-folder",
         str(project), "--orchestrator-root", str(REPO_ROOT)],
        capture_output=True, text=True, cwd=REPO_ROOT, check=True, env=child_env())
    reply = json.loads(proc.stdout)
    assert reply["ok"] is True and ".claude/agents/a.md" in reply["removed"]


def test_strip_shipped_hooks_keeps_a_shipped_command_with_a_users_argument():
    edited = _cmd("ensure-containers.sh") + " --strict"
    hooks = {"SessionStart": [{"hooks": [{"type": "command", "command": edited}]}]}
    assert strip_shipped_hooks(hooks, [TEMPLATE["hooks"]]) == (hooks, [])


# ── collections ──────────────────────────────────────────────────────────

class _Ident:
    def __init__(self, pid, name, kg, dev, prefix, shared="VibeCodedOrchestrator_KnowledgeGraph"):
        self.project_id, self.name = pid, name
        self.kg_primary, self.development, self.diagrams = kg, dev, None
        self.kg_shared, self.prefix = shared, prefix

    def kg_collections(self):
        return tuple(x for x in (self.kg_primary, self.kg_shared, self.development) if x)

    def code_collections(self):
        return (f"{self.prefix}_CodeModule", f"{self.prefix}_CodeFunction") if self.prefix else ()


class _Snap:
    def __init__(self, *projects, resolvable=True):
        self.projects, self.resolvable = projects, resolvable


def test_the_drop_covers_this_projects_bound_classes_and_keeps_another_projects():
    me = _Ident("p1", "Acme", "AcmeRenamed_KnowledgeGraph", "Acme_Development", "AcmeCode")
    other = _Ident("p2", "Acme", "Acme_KnowledgeGraph", "Other_Development", "Other")
    plan = unregister_collections.plan_drop("Acme", project_id="p1", snapshot=_Snap(me, other))
    assert "AcmeRenamed_KnowledgeGraph" in plan.targets
    assert "AcmeCode_CodeFunction" in plan.targets
    assert "Acme_Development" in plan.targets
    assert "VibeCodedOrchestrator_KnowledgeGraph" not in plan.targets
    assert "Acme_KnowledgeGraph" not in plan.targets
    assert plan.kept == [{"collection": "Acme_KnowledgeGraph",
                          "reason": "also used by the project 'Acme', so it was not dropped"}]


def test_an_unreadable_registry_keeps_the_name_derived_set_and_says_so():
    plan = unregister_collections.plan_drop("Acme", project_id="p1",
                                            snapshot=_Snap(resolvable=False))
    assert plan.targets[:2] == ["Acme_KnowledgeGraph", "Acme_Development"]
    assert plan.notes and plan.kept == []


def test_drop_refuses_the_shared_class_and_reports_what_it_kept(monkeypatch):
    from vco_lib import bootstrap_case_sibling, project_identity

    monkeypatch.setattr(bootstrap_case_sibling, "protected_shared_names",
                        lambda: ("Acme_KnowledgeGraph", {"acme_knowledgegraph"}))
    me = _Ident("p1", "Acme", "Acme_KnowledgeGraph", "Acme_Development", None)
    other = _Ident("p2", "Other", "Other_KnowledgeGraph", "Acme_Development", None)
    monkeypatch.setattr(project_identity, "resolve_snapshot", lambda: _Snap(me, other))
    deleted: list[str] = []
    out = unregister_collections.drop_project_collections(
        "Acme", project_id="p1", delete_class=lambda n, weaviate_url=None: deleted.append(n))
    assert "Acme_KnowledgeGraph" not in deleted and "Acme_Development" not in deleted
    assert "Acme_Diagrams" in deleted
    assert [e["collection"] for e in out["errors"]] == ["Acme_KnowledgeGraph"]
    assert [k["collection"] for k in out["kept"]] == ["Acme_Development"]
