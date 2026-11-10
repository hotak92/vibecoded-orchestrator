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
from vco_lib import git_exclude, project_init, unregister_collections, unregister_files, vscode_excludes
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
    _ship(project, {".claude/hooks/x.sh": b"#!/bin/bash\n"})  # the manifest proves x.sh's line
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


def test_an_unreadable_registry_drops_nothing_and_says_so():
    """Review A-L4: the GUI promises other projects' collections are never
    dropped; with the registry unreadable that cannot be checked, so every
    class is kept with the reason."""
    plan = unregister_collections.plan_drop("Acme", project_id="p1",
                                            snapshot=_Snap(resolvable=False))
    assert plan.targets == []
    assert [k["collection"] for k in plan.kept][:2] == [
        "Acme_KnowledgeGraph", "Acme_Development"]
    assert all("could not be read" in k["reason"] for k in plan.kept)
    assert plan.notes


def test_without_an_id_the_name_derived_set_is_dropped():
    plan = unregister_collections.plan_drop("Acme")
    assert plan.targets[:2] == ["Acme_KnowledgeGraph", "Acme_Development"]
    assert plan.kept == []


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


# ── fix round 1: never reach outside the folder, per-line evidence ────────

def test_a_shipped_file_reached_through_a_symlinked_dir_is_never_deleted(project: Path, tmp_path):
    """R1 F1 (act + leave-alone): a byte-identical shipped file in the
    project's own dir goes; the same file reached through a linked dir (the
    user's tree outside the folder) stays, and the manifest stays too."""
    outside = tmp_path / "shared-tools"
    outside.mkdir()
    (outside / "kg-search").write_bytes(b"#!/bin/bash\n")
    (project / ".claude/scripts").symlink_to(outside, target_is_directory=True)
    _ship(project, {".claude/agents/a.md": b"a\n"})
    manifest = json.loads((project / ".claude/.vco-manifest.json").read_text())
    manifest["files"][".claude/scripts/kg-search"] = {"sha256": sha256_bytes(b"#!/bin/bash\n")}
    (project / ".claude/.vco-manifest.json").write_text(json.dumps(manifest))

    out = _purge(project)

    assert (outside / "kg-search").read_bytes() == b"#!/bin/bash\n"
    assert not (project / ".claude/agents/a.md").exists(), "the real dir's file still goes"
    assert any(k.startswith(".claude/scripts/kg-search — .claude/scripts is a link")
               for k in out["kept"])
    assert (project / ".claude/.vco-manifest.json").exists(), "a retry can still prove"


def test_a_symlinked_claude_dir_keeps_the_real_state_tree_outside(project: Path, tmp_path):
    """R1 F1: ``rmtree`` of ``.claude/state`` never runs through a linked
    ``.claude`` (the act case — a real ``.claude/state`` going — is
    ``test_vco_runtime_files_go_and_user_bytes_stay``)."""
    real = tmp_path / "real-claude"
    (real / "state/scratch/lane").mkdir(parents=True)
    (real / "state/scratch/lane/my-notes.txt").write_text("mine")
    (project / ".claude").rmdir()
    (project / ".claude").symlink_to(real, target_is_directory=True)

    out = _purge(project)

    assert (real / "state/scratch/lane/my-notes.txt").read_text() == "mine"
    assert any(k.startswith(".claude/state — .claude is a link") for k in out["kept"])
    assert out["removed"] == []


def test_a_settings_file_under_a_linked_claude_dir_is_not_rewritten(project: Path, tmp_path):
    real = tmp_path / "real-claude"
    real.mkdir()
    body = json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command",
                                                       "command": _cmd("notify-stop.sh")}]}]}})
    (real / "settings.json").write_text(body)
    (project / ".claude").rmdir()
    (project / ".claude").symlink_to(real, target_is_directory=True)
    out = _purge(project)
    assert (real / "settings.json").read_text() == body
    assert any(k.startswith(".claude/settings.json — .claude is a link") for k in out["kept"])


@pytest.mark.parametrize("rel", ["C:/Users/x/notes.txt", "C:notes.txt", "//host/share/x",
                                 "a/../b", "/etc/passwd", "sub/C:/x"])
def test_a_drive_anchored_or_escaping_manifest_key_is_refused(project: Path, rel: str):
    """R1 F3: on Windows ``joinpath('C:', …)`` resets to the drive."""
    assert unregister_files._inside(project, rel) is None


def test_a_plain_manifest_key_is_accepted(project: Path):
    assert unregister_files._inside(project, ".claude/hooks/x.sh") == project / ".claude/hooks/x.sh"


def test_a_skip_existing_preserved_row_is_not_evidence(project: Path):
    """R1 F4 (both ways): the user's pre-existing file, byte-equal to the
    shipped one, stays; a ``preserve`` row with equal bytes still goes."""
    _ship(project, {".claude/agents/a.md": b"a\n"})
    for name in ("theirs.md", "ours.md"):
        (project / f".claude/agents/{name}").write_text("shipped\n")
    manifest = json.loads((project / ".claude/.vco-manifest.json").read_text())
    sha = sha256_bytes(b"shipped\n")
    manifest["preserved_files"] = {
        ".claude/agents/theirs.md": {"shipped_sha256": sha, "reason": "skip-existing"},
        ".claude/agents/ours.md": {"shipped_sha256": sha, "reason": "preserve"},
    }
    (project / ".claude/.vco-manifest.json").write_text(json.dumps(manifest))
    out = _purge(project)
    assert (project / ".claude/agents/theirs.md").exists()
    assert not (project / ".claude/agents/ours.md").exists()
    assert ".claude/agents/theirs.md" not in out["removed"] + out["kept_modified"]


def test_a_settings_file_holding_only_an_empty_env_is_deleted(project: Path):
    """R1 F6: ``{"env": {}}`` is emptied, so the file goes; a user key keeps it."""
    path = project / ".claude/settings.json"
    path.write_text(json.dumps({"env": {}}))
    out = _purge(project)
    assert not path.exists() and ".claude/settings.json" in out["removed"]
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({"env": {}, "model": "x"}))
    _purge(project)
    assert json.loads(path.read_text()) == {"model": "x"}


def test_a_users_line_inside_an_ended_exclude_block_stays(project: Path):
    """R1 F2 (both ways): VCO's own lines in the block go; the user's line
    edited in before ``# VCO end`` stays, and the reply names it."""
    _ship(project, {".claude/hooks/x.sh": b"#!/bin/bash\n"})
    info = project / ".git/info"
    info.mkdir(parents=True)
    (info / "exclude").write_text("*.local\n")
    git_exclude.append_git_info_exclude(project, ["/.claude/hooks/x.sh", "/.claude/state/"])
    text = (info / "exclude").read_text()
    (info / "exclude").write_text(text.replace("# VCO end", "/my-secret-dir/\n# VCO end"))

    out = _purge(project)

    assert (info / "exclude").read_text() == "*.local\n/my-secret-dir/\n"
    assert any("/my-secret-dir/" in k for k in out["kept"])


def test_strip_keeps_an_entry_it_cannot_prove_and_drops_one_it_can(project: Path):
    info = project / ".git/info"
    info.mkdir(parents=True)
    git_exclude.append_git_info_exclude(project, ["/.claude/hooks/x.sh", "/.claude/state/"])
    out = git_exclude.strip_vco_blocks(project)
    assert (info / "exclude").read_text() == "/.claude/hooks/x.sh\n"
    assert out["kept"] == ["/.claude/hooks/x.sh"]
    (info / "exclude").write_text("")
    git_exclude.append_git_info_exclude(project, ["/.claude/hooks/x.sh", "/.claude/state/"])
    git_exclude.strip_vco_blocks(
        project, vco_entries=git_exclude.vco_block_entries([".claude/hooks/x.sh"], project))
    assert (info / "exclude").read_text() == ""


# ── .vscode/settings.json (owed item 3) ──────────────────────────────────

def test_a_vscode_settings_file_vco_created_goes_whole(project: Path):
    """VCO created it (marker) and only VCO's keys are in it → the file and
    the emptied ``.vscode/`` go."""
    vscode_excludes.backfill_in_project(project)
    out = _purge(project)
    assert not (project / ".vscode").exists()
    assert ".vscode/settings.json" in out["removed"]


def test_a_vscode_file_vco_created_keeps_the_users_keys_and_edited_values(project: Path):
    vscode_excludes.backfill_in_project(project)
    path = project / ".vscode/settings.json"
    data = json.loads(path.read_text())
    data["editor.tabSize"] = 2
    data["search.exclude"]["**/my-big-dir"] = True
    path.write_text(json.dumps(data))
    out = _purge(project)
    assert json.loads(path.read_text()) == {"search.exclude": data["search.exclude"],
                                            "editor.tabSize": 2}
    assert any(k.startswith(".vscode/settings.json search.exclude") for k in out["kept"])


def test_a_users_vscode_file_loses_only_vcos_keys_and_is_never_deleted(project: Path):
    """The user had the file (no marker): VCO's keys go, the file stays even
    when nothing is left; the legacy (pre-v0.2.103) files.exclude block is
    VCO's too; a key the user wrote with a different value stays."""
    path = project / ".vscode/settings.json"
    path.parent.mkdir()
    path.write_text('{\n  // mine\n  "python.analysis.indexing": true\n}\n')
    vscode_excludes.backfill_in_project(project)
    data = jsonc_load(path)
    data["files.exclude"] = dict(vscode_excludes._LEGACY_FILES_EXCLUDE_BLOCK)
    path.write_text("{\n  // mine\n" + json.dumps(data)[1:])
    out = _purge(project)
    assert jsonc_load(path) == {"python.analysis.indexing": True}
    assert "// mine" in path.read_text()
    assert any("python.analysis.indexing" in k for k in out["kept"])

    path.write_text(json.dumps({"files.watcherExclude":
                                vscode_excludes._VSCODE_EXCLUDE_DEFAULTS["files.watcherExclude"]}))
    _purge(project)
    assert path.exists() and json.loads(path.read_text()) == {}


def test_a_linked_vscode_settings_file_is_not_rewritten(project: Path, tmp_path):
    target = tmp_path / "shared-vscode.json"
    vscode_excludes.backfill_in_project(project)
    path = project / ".vscode/settings.json"
    path.rename(target)
    path.symlink_to(target)
    body = target.read_text()
    out = _purge(project)
    assert target.read_text() == body and path.is_symlink()
    assert any(k.startswith(".vscode/settings.json — it is a link") for k in out["kept"])


def jsonc_load(path: Path) -> dict:
    from vco_lib import jsonc_edit
    loaded = jsonc_edit.load_object(path)
    assert loaded is not None
    return loaded[0]


# ── re-add after Remove (v0.2.106 field request: "re-activation") ─────────

def _manifest_files(folder: Path) -> dict:
    return json.loads((folder / ".claude" / ".vco-manifest.json").read_text())["files"]


@pytest.mark.parametrize("user_edits_a_file", [False, True])
def test_a_removed_folder_can_be_added_again_with_the_real_bundle(
    project: Path, user_edits_a_file: bool
):
    """Add (the real bundle engine, first-install mode) → Remove → Add again.

    The second add must leave the folder exactly as fully installed as the
    first: every shipped file back with the shipped bytes, a fresh manifest,
    VCO's hook registrations back in ``settings.json`` — and the file the user
    changed before the removal (kept by the purge) is still theirs.
    """
    first = project_init.install_project_bundle(project, orchestrator_root=REPO_ROOT)
    assert first.get("ok", True) is not False, first
    shipped = _manifest_files(project)
    assert shipped, "the first add recorded shipped files"
    edited_rel = next(r for r in sorted(shipped) if r.startswith(".claude/agents/"))
    if user_edits_a_file:
        (project / edited_rel).write_text("my edited agent\n")
    else:
        edited_rel = ""

    out = _purge(project)
    assert not out["errors"], out
    if user_edits_a_file:
        assert edited_rel in out["kept_modified"]
    else:
        assert not (project / ".claude" / ".vco-manifest.json").exists(), "nothing kept: manifest goes"
    # knowledge/** is always the user's data: the purge never touches it.
    gone = [r for r in shipped if r != edited_rel and not r.startswith("knowledge/")]
    assert gone and all(not (project / r).exists() for r in gone)
    settings_path = project / ".claude" / "settings.json"
    assert not settings_path.exists() or not json.loads(settings_path.read_text()).get("hooks"), (
        "the removal took VCO's hook registrations out"
    )

    second = project_init.install_project_bundle(project, orchestrator_root=REPO_ROOT)
    assert second.get("ok", True) is not False, second
    again = _manifest_files(project)
    for rel, rec in again.items():
        if rel == edited_rel:
            continue
        p = project / rel
        assert p.is_file(), f"{rel} not re-installed"
        assert sha256_bytes(p.read_bytes()) == rec["sha256"], rel
    assert set(shipped) - {edited_rel} <= set(again), "every shipped file is back"
    if user_edits_a_file:
        assert (project / edited_rel).read_text() == "my edited agent\n", "the user's file stays theirs"
    settings = json.loads((project / ".claude" / "settings.json").read_text())
    assert settings.get("hooks"), "VCO's hook registrations are back"
