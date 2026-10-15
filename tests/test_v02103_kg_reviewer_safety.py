# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.103 — the post-commit KG reviewer's safety boundaries.

Incident (a user project): the background reviewer rewrote a "do not change
without discussion" decision node and stripped its frontmatter, edited an
ADR under ``docs/``, minted duplicate / stale status nodes from a partial
diff, and left its edits uncommitted where ``git commit -a`` swept them into
an unrelated commit. Owner rulings 2026-10-15:

* model: ONE machine-wide setting, default Sonnet (Haiku / Sonnet / Opus);
  when unavailable, only NON-flash gateway rungs, else skip;
* research-first prompt, KG only (no docs job), capped diff with a
  truncation note;
* ENFORCED boundaries: knowledge/-only tool scope + a post-run validator
  that reverts forbidden edits from a snapshot copy;
* isolation: only the reviewer's own files are committed, as
  ``kg-sync: <hash> <subject>``, never with the user's work; skip
  conditions; a recursion guard; no empty commit.

Every test drives the real code against a temp git repo; the ``claude`` CLI
is always FAKED (never a real model), and Weaviate is the unroutable
sentinel the suite pins.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import types
from pathlib import Path
from typing import Callable, List, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "templates" / "scripts"
LIVE_SHAPE = (REPO_ROOT / "tests" / "fixtures" / "kg_reviewer"
              / "gateway-models-live-shape.json")

for _p in (str(REPO_ROOT), str(SCRIPTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from vco_lib import kg_reviewer_guard as guard  # noqa: E402
from vco_lib import kg_reviewer_model as model_mod  # noqa: E402
from vco_lib import kg_reviewer_prompt as prompt_mod  # noqa: E402

_REAL_RUN = subprocess.run

LOCKED_NODE = """---
title: Storage decision
type: concept
tags: [decision, storage]
status: active
updated: 2026-01-01T00:00:00Z
---

# Storage decision

We store blobs on disk. Provenance: chosen in the first design review.
"""

PLAIN_NODE = """---
title: Cache layer
type: concept
tags: [cache]
status: active
updated: 2026-01-01T00:00:00Z
---

# Cache layer

The cache keeps 100 entries.
"""

ADR = "# ADR 1\n\nAccepted: blobs on disk.\n"


# ── fixtures ─────────────────────────────────────────────────────────────


def _git(repo: Path, *args: str) -> str:
    return _REAL_RUN(["git", *args], cwd=repo, check=True,
                     capture_output=True, text=True).stdout


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for k, v in {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                 "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                 "GIT_COMMITTER_NAME": "t",
                 "GIT_COMMITTER_EMAIL": "t@example.invalid"}.items():
        monkeypatch.setenv(k, v)
    r = tmp_path / "proj"
    (r / "knowledge" / "concepts").mkdir(parents=True)
    (r / "docs" / "adr").mkdir(parents=True)
    (r / "knowledge" / "concepts" / "storage-decision.md").write_text(LOCKED_NODE)
    (r / "knowledge" / "concepts" / "cache-layer.md").write_text(PLAIN_NODE)
    (r / "docs" / "adr" / "0001.md").write_text(ADR)
    (r / "src.py").write_text("x = 1\n")
    _git(r, "init", "-q")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "base")
    (r / "src.py").write_text("x = 2\n")
    _git(r, "commit", "-q", "-am", "feat: bump x")
    return r


def _load(name: str, path: Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def chain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    sb = _load("_v02103_summary_backends", SCRIPTS_DIR / "summary_backends.py")
    sb.reset_backend_cache()
    monkeypatch.setattr(sb, "_breaker_path", lambda: tmp_path / "breaker.json")
    mod = _load("_v02103_kg_reviewer_backend",
                SCRIPTS_DIR / "kg_reviewer_backend.py")
    setattr(mod, "_sb", sb)
    for k in ("VCO_KG_REVIEW_MAX_TURNS", "VCO_KG_REVIEW_MODEL"):
        monkeypatch.delenv(k, raising=False)
    yield mod
    sb.reset_backend_cache()


def _live_ids() -> frozenset:
    data = json.loads(LIVE_SHAPE.read_text(encoding="utf-8"))
    return frozenset(row["id"] for row in data["data"])


class _Rig:
    def __init__(self, chain, monkeypatch, *, claude=True,
                 catalogue=frozenset(), consent=False):
        self.lines: List[str] = []
        self.chain = chain
        self.latched: dict = {}
        monkeypatch.setattr(chain.shutil, "which",
                            lambda cmd: "/usr/bin/claude" if claude else None)
        monkeypatch.setattr(chain, "gateway_catalogue",
                            lambda timeout_s=2.0: ("http://127.0.0.1:11460",
                                                   catalogue, None))
        monkeypatch.setattr(chain._sb, "breaker_state", self.latched.get)
        monkeypatch.setattr(chain._sb, "openai_consent_granted",
                            lambda: consent)
        monkeypatch.setattr(chain._sb, "openai_available", lambda: True)
        monkeypatch.setattr(chain._model, "_preferences_value", lambda: None)

    def latch(self, *tiers: str) -> None:
        for t in tiers:
            self.latched[t] = {"reason": "quota", "remaining": 100}

    def decide(self):
        return self.chain.select_reviewer_backend(log=self.lines.append)


# ── stream-json transcript helpers (the fake claude's output) ────────────


def _tool_use(i: int, name: str, **inp) -> dict:
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": f"t{i}", "name": name, "input": inp}]}}


def _tool_result(i: int, text: str) -> dict:
    return {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": f"t{i}", "content": text}]}}


_RESULT = {"type": "result", "subtype": "success", "num_turns": 5,
           "result": "done"}


def _stream(*events: dict) -> str:
    return "\n".join(json.dumps(e) for e in
                     ({"type": "system", "subtype": "init"}, *events, _RESULT))


# ═════════════════════════════════════════════════════════════════════════
# 1. Prompt
# ═════════════════════════════════════════════════════════════════════════


def test_prompt_demands_research_first_and_has_no_docs_job(repo):
    commit = prompt_mod.collect_commit(repo)
    text = prompt_mod.build_prompt(commit, repo, today="2026-10-15")
    # research steps, in order
    order = ["hybrid_search", "Read the FULL current text",
             "full files, not only the diff", "prefer the NEWEST source",
             "extending an existing node", "Write only facts",
             '"blocker", "in-progress"', "When unsure about any of this, write nothing"]
    positions = [text.find(s) for s in order]
    assert all(p >= 0 for p in positions), list(zip(order, positions))
    assert positions == sorted(positions), list(zip(order, positions))
    assert "Skip trivial" in text and "test-only" in text
    # KG only: docs/ appears only in the prohibition, never as a job
    assert "never edit anything outside" in text
    assert "not docs/" in text
    lowered = text.lower()
    for job in ("and documentation", "update them", "docs (in",
                "docs reference behavior"):
        assert job not in lowered, job
    assert "## Proposed change (kg-reviewer, 2026-10-15)" in text
    assert 'scope="shared"' in text  # forbidden explicitly
    assert "+x = 2" in text and "feat: bump x" in text


def test_prompt_diff_is_capped_above_300_lines_with_a_truncation_note(repo):
    big = repo / "big.txt"
    big.write_text("".join(f"line-{i:05d}\n" for i in range(5000)))
    _git(repo, "add", "big.txt")
    _git(repo, "commit", "-q", "-m", "big")
    commit = prompt_mod.collect_commit(repo)
    cap = model_mod.config()["prompt"]["diff_max_lines"]
    assert cap > 300
    assert commit.diff_truncated and commit.diff_lines == cap
    assert "+line-00000" in commit.diff and "line-04999" not in commit.diff
    text = prompt_mod.build_prompt(commit, repo)
    assert "TRUNCATED" in text and "Read the changed files" in text


def test_prompt_untruncated_diff_says_complete_and_root_commit_has_its_diff(
        tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    r = tmp_path / "root"
    r.mkdir()
    _git(r, "init", "-q")
    (r / "a.txt").write_text("root-marker\n")
    _git(r, "add", "a.txt")
    _git(r, "-c", "user.name=t", "-c", "user.email=t@e.invalid", "commit",
         "-q", "-m", "root")
    commit = prompt_mod.collect_commit(r)
    assert "+root-marker" in commit.diff and not commit.diff_truncated
    assert "complete" in prompt_mod.build_prompt(commit, r)


# ═════════════════════════════════════════════════════════════════════════
# 2. Tool scope
# ═════════════════════════════════════════════════════════════════════════


def test_spawn_argv_scopes_writes_to_knowledge(chain):
    cmd = chain.build_claude_cmd("sonnet", 30)
    allowed = cmd[cmd.index("--allowedTools") + 1].split(",")
    denied = cmd[cmd.index("--disallowedTools") + 1].split(",")
    assert "Edit(knowledge/**)" in allowed and "Write(knowledge/**)" in allowed
    assert "Edit" not in allowed and "Write" not in allowed
    assert {"Read", "Glob", "Grep"} <= set(allowed)
    for d in ("Bash", "Edit(docs/**)", "Write(docs/**)", "Edit(.git/**)"):
        assert d in denied
    assert cmd[cmd.index("--permission-mode") + 1] == "dontAsk"
    assert cmd[cmd.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in cmd
    assert cmd[cmd.index("--model") + 1] == "sonnet"


# ═════════════════════════════════════════════════════════════════════════
# 3. Model resolution
# ═════════════════════════════════════════════════════════════════════════


def test_model_default_is_sonnet():
    assert model_mod.resolve(env={}, preferences=lambda: None) == (
        "sonnet", "default")
    assert model_mod.model_choices() == ("haiku", "sonnet", "opus")


def test_model_preference_and_env_override():
    assert model_mod.resolve(env={}, preferences=lambda: "haiku") == (
        "haiku", "preferences")
    assert model_mod.resolve(env={"VCO_KG_REVIEW_MODEL": "Opus"},
                             preferences=lambda: "haiku") == ("opus", "env")


def test_model_invalid_values_are_ignored_with_a_line():
    lines: List[str] = []
    got = model_mod.resolve(env={"VCO_KG_REVIEW_MODEL": "gpt-x"},
                            preferences=lambda: "flash", log=lines.append)
    assert got == ("sonnet", "default")
    assert any("gpt-x" in ln for ln in lines) and any("flash" in ln for ln in lines)


def test_model_reads_the_real_launcher_db_preference(tmp_path, monkeypatch):
    from tests.common.launcher_db_fixture import make_launcher_db
    db = make_launcher_db(tmp_path, app_state={"kg_reviewer.model": "opus"})
    monkeypatch.setenv("VCT_LAUNCHER_DB_PATH", str(db))
    monkeypatch.delenv("VCO_KG_REVIEW_MODEL", raising=False)
    assert model_mod.resolve() == ("opus", "preferences")


def test_chain_runs_the_chosen_model(chain, monkeypatch):
    rig = _Rig(chain, monkeypatch, catalogue=_live_ids())
    d = rig.decide()
    assert (d["mode"], d["model"]) == ("claude", "sonnet"), rig.lines
    monkeypatch.setenv("VCO_KG_REVIEW_MODEL", "opus")
    assert rig.decide()["model"] == "opus"


def test_unavailable_model_falls_back_to_glm_53_not_a_flash(chain, monkeypatch):
    rig = _Rig(chain, monkeypatch, catalogue=_live_ids())
    rig.latch("kg_reviewer:sonnet")
    d = rig.decide()
    assert d["model"] == "claude-gw/glm-5.3[1m]", (d, rig.lines)
    rig.latch("kg_reviewer:claude-gw/glm-5.3")
    assert rig.decide()["model"] == "claude-gw/kimi/k3[1m]", rig.lines


def test_flash_only_gateway_means_skip(chain, monkeypatch):
    flash = frozenset(i for i in _live_ids() if "flash" in i)
    rig = _Rig(chain, monkeypatch, catalogue=flash)
    rig.latch("kg_reviewer:sonnet")
    d = rig.decide()
    assert d["mode"] == "skip", (d, rig.lines)
    assert any("no flash-model fallback" in ln for ln in rig.lines)
    assert all("flash" not in r for r in chain.gateway_rungs())


def test_every_comparable_rung_down_keeps_the_openai_consent_gate(
        chain, monkeypatch):
    rig = _Rig(chain, monkeypatch, catalogue=_live_ids(), consent=True)
    rig.latch("kg_reviewer:sonnet", "kg_reviewer:claude-gw/glm-5.3",
              "kg_reviewer:claude-gw/kimi/k3")
    assert rig.decide()["mode"] == "openai"
    rig2 = _Rig(chain, monkeypatch, catalogue=_live_ids(), consent=False)
    rig2.latch("kg_reviewer:sonnet", "kg_reviewer:claude-gw/glm-5.3",
               "kg_reviewer:claude-gw/kimi/k3")
    assert rig2.decide()["mode"] == "skip"


# ═════════════════════════════════════════════════════════════════════════
# 4. Validator — each rule: act + leave-alone
# ═════════════════════════════════════════════════════════════════════════


def _validate(repo: Path, attributed: Optional[set], snap) -> tuple:
    lines: List[str] = []
    resynced: List[List[str]] = []
    res = guard.validate(snap, attributed, lines.append,
                         resync=resynced.append)
    return res, lines, resynced


def _abs(repo: Path, *rels: str) -> set:
    return {(repo / r).resolve() for r in rels}


def test_rule_outside_knowledge_is_reverted(repo):
    snap = guard.take_snapshot(repo)
    try:
        (repo / "docs/adr/0001.md").write_text("# ADR 1\n\nRewritten.\n")
        res, lines, _ = _validate(repo, _abs(repo, "docs/adr/0001.md"), snap)
        assert (repo / "docs/adr/0001.md").read_text() == ADR
        assert res.reverted[0][0] == "docs/adr/0001.md"
        assert any("REVERTED docs/adr/0001.md: outside knowledge/" in ln
                   for ln in lines)
    finally:
        snap.cleanup()


def test_rule_outside_knowledge_leaves_the_users_concurrent_edit(repo):
    snap = guard.take_snapshot(repo)
    try:
        (repo / "docs/adr/0001.md").write_text("user edit\n")  # not attributed
        res, _lines, _ = _validate(repo, set(), snap)
        assert (repo / "docs/adr/0001.md").read_text() == "user edit\n"
        assert res.reverted == [] and res.kept == []
    finally:
        snap.cleanup()


def test_rule_new_file_outside_knowledge_is_removed(repo):
    snap = guard.take_snapshot(repo)
    try:
        (repo / "notes.md").write_text("made up\n")
        res, _l, _ = _validate(repo, _abs(repo, "notes.md"), snap)
        assert not (repo / "notes.md").exists()
        assert res.reverted
    finally:
        snap.cleanup()


def test_rule_users_dirty_file_is_restored_to_the_users_bytes(repo):
    (repo / "src.py").write_text("x = 'user wip'\n")
    snap = guard.take_snapshot(repo)
    try:
        (repo / "src.py").write_text("x = 'reviewer'\n")
        _validate(repo, _abs(repo, "src.py"), snap)
        assert (repo / "src.py").read_text() == "x = 'user wip'\n"
    finally:
        snap.cleanup()


def test_rule_removed_frontmatter_is_reverted(repo):
    node = repo / "knowledge/concepts/cache-layer.md"
    snap = guard.take_snapshot(repo)
    try:
        node.write_text("# Cache layer\n\nThe cache keeps 200 entries.\n")
        res, lines, resynced = _validate(repo, _abs(repo, str(node.relative_to(repo))), snap)
        assert node.read_text() == PLAIN_NODE
        assert "removed the node's YAML frontmatter" in res.reverted[0][1]
        assert resynced == [["knowledge/concepts/cache-layer.md"]]
    finally:
        snap.cleanup()


def test_rule_updated_key_and_body_edit_is_allowed(repo):
    node = repo / "knowledge/concepts/cache-layer.md"
    snap = guard.take_snapshot(repo)
    try:
        new = PLAIN_NODE.replace("2026-01-01T00:00:00Z", "2026-10-15T00:00:00Z") \
                        .replace("100 entries", "200 entries")
        node.write_text(new)
        res, _l, resynced = _validate(repo, _abs(repo, "knowledge/concepts/cache-layer.md"), snap)
        assert node.read_text() == new
        assert res.kept == ["knowledge/concepts/cache-layer.md"]
        assert res.reverted == [] and resynced == []
    finally:
        snap.cleanup()


def test_rule_rewritten_frontmatter_key_is_reverted(repo):
    node = repo / "knowledge/concepts/cache-layer.md"
    snap = guard.take_snapshot(repo)
    try:
        node.write_text(PLAIN_NODE.replace("status: active", "status: blocked"))
        res, _l, _ = _validate(repo, _abs(repo, "knowledge/concepts/cache-layer.md"), snap)
        assert node.read_text() == PLAIN_NODE
        assert "keys: status" in res.reverted[0][1]
    finally:
        snap.cleanup()


def test_rule_locked_node_rewrite_is_reverted(repo):
    node = repo / "knowledge/concepts/storage-decision.md"
    snap = guard.take_snapshot(repo)
    try:
        node.write_text(LOCKED_NODE.replace("first design review",
                                            "a later meeting"))
        res, _l, _ = _validate(repo, _abs(repo, "knowledge/concepts/storage-decision.md"), snap)
        assert node.read_text() == LOCKED_NODE
        assert "locked node (tagged decision)" in res.reverted[0][1]
    finally:
        snap.cleanup()


def test_rule_locked_node_appended_proposal_is_allowed(repo):
    node = repo / "knowledge/concepts/storage-decision.md"
    snap = guard.take_snapshot(repo)
    try:
        new = (LOCKED_NODE.replace("2026-01-01T00:00:00Z", "2026-10-15T00:00:00Z")
               + "\n## Proposed change (kg-reviewer, 2026-10-15)\n\n"
                 "Blobs moved to object storage (src/store.py).\n")
        node.write_text(new)
        res, _l, _ = _validate(repo, _abs(repo, "knowledge/concepts/storage-decision.md"), snap)
        assert node.read_text() == new and res.kept
    finally:
        snap.cleanup()


def test_rule_locked_by_body_marker_and_undated_proposal_is_reverted(repo):
    node = repo / "knowledge/concepts/cache-layer.md"
    locked = PLAIN_NODE + "\nDo not change without discussion.\n"
    node.write_text(locked)
    snap = guard.take_snapshot(repo)
    try:
        node.write_text(locked + "\n## Proposed change (kg-reviewer, soon)\nx\n")
        res, _l, _ = _validate(repo, _abs(repo, "knowledge/concepts/cache-layer.md"), snap)
        assert node.read_text() == locked
        assert "locked node" in res.reverted[0][1]
    finally:
        snap.cleanup()


def test_rule_deleted_node_is_restored_and_new_node_kept(repo):
    old = repo / "knowledge/concepts/cache-layer.md"
    new = repo / "knowledge/concepts/new-topic.md"
    snap = guard.take_snapshot(repo)
    try:
        old.unlink()
        new.write_text("---\ntitle: New topic\n---\n\nFact.\n")
        res, _l, _ = _validate(repo, _abs(repo, "knowledge/concepts/cache-layer.md",
                                          "knowledge/concepts/new-topic.md"), snap)
        assert old.read_text() == PLAIN_NODE
        assert res.reverted[0] == ("knowledge/concepts/cache-layer.md", "deleted a node")
        assert res.kept == ["knowledge/concepts/new-topic.md"]
    finally:
        snap.cleanup()


def test_rule_non_node_file_in_knowledge_is_reverted_sidecar_left(repo):
    snap = guard.take_snapshot(repo)
    try:
        (repo / "knowledge/scratch.txt").write_text("x\n")
        (repo / "knowledge/.node_formats.json").write_text("{}\n")
        res, _l, _ = _validate(repo, _abs(repo, "knowledge/scratch.txt",
                                          "knowledge/.node_formats.json"), snap)
        assert not (repo / "knowledge/scratch.txt").exists()
        assert (repo / "knowledge/.node_formats.json").read_text() == "{}\n"
        assert [r for r, _ in res.reverted] == ["knowledge/scratch.txt"]
    finally:
        snap.cleanup()


def test_rule_unattributed_run_still_reverts_violations(repo):
    node = repo / "knowledge/concepts/storage-decision.md"
    snap = guard.take_snapshot(repo)
    try:
        node.write_text("no frontmatter\n")
        res, _l, _ = _validate(repo, None, snap)
        assert node.read_text() == LOCKED_NODE and res.reverted
    finally:
        snap.cleanup()


def test_attribution_reads_tool_inputs_and_store_results(repo):
    out = _stream(
        _tool_use(1, "Write", file_path=str(repo / "knowledge/a.md"), content="x"),
        _tool_use(2, "Edit", file_path="knowledge/b.md"),
        _tool_use(3, "mcp__weaviate-kg__store_knowledge_node", title="T",
                  content="c"),
        _tool_result(3, json.dumps({"success": True,
                                    "absolute_path": str(repo / "knowledge/c.md")})),
        _tool_use(4, "Read", file_path=str(repo / "src.py")),
    )
    got = guard.attributed_paths(out, repo)
    assert got == _abs(repo, "knowledge/a.md", "knowledge/b.md", "knowledge/c.md")
    assert guard.attributed_paths(json.dumps(_RESULT), repo) is None


# ═════════════════════════════════════════════════════════════════════════
# 5. Commit isolation
# ═════════════════════════════════════════════════════════════════════════


def _user_work(repo: Path) -> None:
    """Staged + unstaged + untracked user work the commit must not touch."""
    (repo / "src.py").write_text("x = 'staged'\n")
    _git(repo, "add", "src.py")
    (repo / "src.py").write_text("x = 'staged+unstaged'\n")
    (repo / "docs/adr/0001.md").write_text(ADR + "user unstaged\n")
    (repo / "untracked.txt").write_text("u\n")


def _commit(repo, snap, kept, attributed=True) -> tuple:
    lines: List[str] = []
    sha = guard.commit_reviewer_changes(
        snap, kept, short="abc1234", subject="feat: bump x", model="sonnet",
        attributed=attributed, log=lines.append)
    return sha, lines


def test_commit_contains_only_the_reviewers_files(repo):
    _user_work(repo)
    staged_before = _git(repo, "diff", "--cached", "--name-only")
    snap = guard.take_snapshot(repo)
    try:
        (repo / "knowledge/concepts/new-topic.md").write_text("---\ntitle: N\n---\nF.\n")
        (repo / "knowledge/concepts/cache-layer.md").write_text(
            PLAIN_NODE.replace("100", "200"))
        sha, lines = _commit(repo, snap, ["knowledge/concepts/new-topic.md",
                                          "knowledge/concepts/cache-layer.md"])
    finally:
        snap.cleanup()
    assert sha, lines
    files = set(_git(repo, "show", "--name-only", "--format=", sha).split())
    assert files == {"knowledge/concepts/new-topic.md",
                     "knowledge/concepts/cache-layer.md"}
    assert _git(repo, "log", "-1", "--format=%s").strip() == \
        "kg-sync: abc1234 feat: bump x"
    # the user's work is exactly where it was
    assert _git(repo, "diff", "--cached", "--name-only") == staged_before
    assert _git(repo, "show", ":src.py") == "x = 'staged'\n"
    assert (repo / "src.py").read_text() == "x = 'staged+unstaged'\n"
    assert "user unstaged" in (repo / "docs/adr/0001.md").read_text()
    assert "?? untracked.txt" in _git(repo, "status", "--porcelain")


@pytest.mark.parametrize("make_block, needle", [
    (lambda r: (r / ".git" / "MERGE_HEAD").write_text("0" * 40), "MERGE_HEAD"),
    (lambda r: (r / ".git" / "CHERRY_PICK_HEAD").write_text("0" * 40),
     "CHERRY_PICK_HEAD"),
    (lambda r: (r / ".git" / "rebase-merge").mkdir(), "rebase-merge"),
    (lambda r: (r / ".git" / "BISECT_LOG").write_text(""), "BISECT_LOG"),
    (lambda r: (r / ".git" / "index.lock").write_text(""), "index.lock"),
])
def test_commit_skipped_while_git_is_busy(repo, make_block, needle):
    # A TRACKED node, so no `git add` runs first: the skip must come from
    # the reviewer's own check, not from git refusing a later step.
    node = repo / "knowledge/concepts/cache-layer.md"
    snap = guard.take_snapshot(repo)
    head = _git(repo, "rev-parse", "HEAD")
    try:
        node.write_text(PLAIN_NODE.replace("100", "300"))
        make_block(repo)
        sha, lines = _commit(repo, snap, ["knowledge/concepts/cache-layer.md"])
    finally:
        snap.cleanup()
    assert sha is None
    assert _git(repo, "rev-parse", "HEAD") == head
    assert any("commit skipped" in ln and needle in ln for ln in lines), lines
    assert "300" in node.read_text()  # the files are left in place


def test_commit_skipped_when_head_moved(repo):
    snap = guard.take_snapshot(repo)
    try:
        (repo / "src.py").write_text("x = 3\n")
        _git(repo, "commit", "-q", "-am", "user commit meanwhile")
        (repo / "knowledge/concepts/new-topic.md").write_text("x\n")
        sha, lines = _commit(repo, snap, ["knowledge/concepts/new-topic.md"])
    finally:
        snap.cleanup()
    assert sha is None and any("HEAD moved" in ln for ln in lines)


def test_commit_skipped_when_target_had_user_changes(repo):
    node = repo / "knowledge/concepts/cache-layer.md"
    node.write_text(PLAIN_NODE + "\nuser wip\n")
    snap = guard.take_snapshot(repo)
    try:
        node.write_text(PLAIN_NODE + "\nuser wip\nreviewer line\n")
        sha, lines = _commit(repo, snap, ["knowledge/concepts/cache-layer.md"])
    finally:
        snap.cleanup()
    assert sha is None
    assert any("already had uncommitted changes" in ln for ln in lines)


def test_no_empty_commit_and_unattributed_run_not_committed(repo):
    head = _git(repo, "rev-parse", "HEAD")
    snap = guard.take_snapshot(repo)
    try:
        sha, lines = _commit(repo, snap, [])
        assert sha is None and any("wrote nothing" in ln for ln in lines)
        # a kept path whose bytes are back to the start is not a change
        sha, _ = _commit(repo, snap, ["knowledge/concepts/cache-layer.md"])
        assert sha is None
        (repo / "knowledge/concepts/new-topic.md").write_text("x\n")
        sha, lines = _commit(repo, snap, ["knowledge/concepts/new-topic.md"],
                             attributed=False)
        assert sha is None and any("could not be attributed" in ln for ln in lines)
    finally:
        snap.cleanup()
    assert _git(repo, "rev-parse", "HEAD") == head


def test_ignored_knowledge_is_not_committed(repo):
    (repo / ".gitignore").write_text("/knowledge/new-*.md\n")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-q", "-m", "ignore")
    head = _git(repo, "rev-parse", "HEAD")
    snap = guard.take_snapshot(repo)
    try:
        (repo / "knowledge/new-x.md").write_text("x\n")
        sha, lines = _commit(repo, snap, ["knowledge/new-x.md"])
    finally:
        snap.cleanup()
    assert sha is None and _git(repo, "rev-parse", "HEAD") == head
    assert any("git-ignored" in ln for ln in lines)


def test_a_rejecting_user_hook_is_respected_and_index_restored(repo):
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho 'policy says no' >&2\nexit 1\n")
    hook.chmod(0o755)
    snap = guard.take_snapshot(repo)
    try:
        (repo / "knowledge/concepts/new-topic.md").write_text("x\n")
        sha, lines = _commit(repo, snap, ["knowledge/concepts/new-topic.md"])
    finally:
        snap.cleanup()
    assert sha is None and any("policy says no" in ln for ln in lines)
    # our `git add` of the new file was undone; the file stays untracked
    assert "?? knowledge/concepts/new-topic.md" in _git(repo, "status", "--porcelain")


# ═════════════════════════════════════════════════════════════════════════
# 6. End to end through `run` (fake claude) + recursion guard
# ═════════════════════════════════════════════════════════════════════════


def _fake_subprocess(behaviour: Callable[[dict], str], calls: list):
    def run(cmd, *a, **kw):
        if cmd and cmd[0] == "claude":
            calls.append({"cmd": list(cmd), **kw})
            return subprocess.CompletedProcess(cmd, 0, stdout=behaviour(kw),
                                               stderr="")
        return _REAL_RUN(cmd, *a, **kw)
    return types.SimpleNamespace(run=run, TimeoutExpired=subprocess.TimeoutExpired,
                                 CompletedProcess=subprocess.CompletedProcess,
                                 DEVNULL=subprocess.DEVNULL, PIPE=subprocess.PIPE)


def _run_chain(chain, monkeypatch, repo, behaviour, calls) -> str:
    monkeypatch.setattr(chain, "subprocess", _fake_subprocess(behaviour, calls))
    monkeypatch.setattr(chain, "select_reviewer_backend",
                        lambda log=None, model=None: {
                            "mode": "claude", "model": "sonnet",
                            "base_url": None, "reason": "t"})
    monkeypatch.setattr(chain, "_record_outcome", lambda *a, **k: None)
    log = repo.parent / "review.log"
    chain.main(["run", "--project-root", str(repo), "--log", str(log)])
    return log.read_text(encoding="utf-8") if log.exists() else ""


def test_run_end_to_end_reverts_the_incident_and_commits_only_good_nodes(
        chain, monkeypatch, repo):
    _user_work(repo)
    reviewed = _git(repo, "rev-parse", "HEAD").strip()

    def behaviour(kw) -> str:
        # what the incident's reviewer did, plus one legitimate node
        dec = repo / "knowledge/concepts/storage-decision.md"
        dec.write_text("# Storage decision\n\nProvenance: a later meeting.\n")
        (repo / "docs/adr/0001.md").write_text("# ADR 1\n\nreviewer edit\n")
        good = repo / "knowledge/concepts/blob-store.md"
        good.write_text("---\ntitle: Blob store\n---\n\nx is 2.\n")
        return _stream(
            _tool_use(1, "Write", file_path=str(dec), content="..."),
            _tool_use(2, "Edit", file_path=str(repo / "docs/adr/0001.md")),
            _tool_use(3, "Write", file_path=str(good), content="..."))

    calls: list = []
    log = _run_chain(chain, monkeypatch, repo, behaviour, calls)
    assert calls and calls[0]["cwd"] == repo.resolve()
    assert "feat: bump x" in calls[0]["input"]
    assert (repo / "knowledge/concepts/storage-decision.md").read_text() == LOCKED_NODE
    assert (repo / "docs/adr/0001.md").read_text() == ADR + "user unstaged\n"
    head = _git(repo, "rev-parse", "HEAD").strip()
    assert head != reviewed
    files = _git(repo, "show", "--name-only", "--format=", head).split()
    assert files == ["knowledge/concepts/blob-store.md"]
    assert "REVERTED knowledge/concepts/storage-decision.md" in log
    assert "REVERTED docs/adr/0001.md" in log
    assert "committed" in log
    assert _git(repo, "show", ":src.py") == "x = 'staged'\n"


def test_run_that_writes_nothing_makes_no_commit(chain, monkeypatch, repo):
    head = _git(repo, "rev-parse", "HEAD")
    calls: list = []
    log = _run_chain(chain, monkeypatch, repo, lambda kw: _stream(), calls)
    assert calls and _git(repo, "rev-parse", "HEAD") == head
    assert "wrote nothing" in log


def test_reviewer_commits_are_never_reviewed(chain, monkeypatch, repo):
    (repo / "knowledge/concepts/x.md").write_text("x\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "kg-sync: abc1234 feat: bump x")
    calls: list = []
    log = _run_chain(chain, monkeypatch, repo, lambda kw: _stream(), calls)
    assert calls == [], "a kg-sync: commit must not spawn a reviewer"
    assert "not reviewed" in log


def test_run_with_unattributable_output_commits_nothing(chain, monkeypatch, repo):
    head = _git(repo, "rev-parse", "HEAD")

    def behaviour(kw) -> str:
        (repo / "knowledge/concepts/blob-store.md").write_text("---\ntitle: B\n---\nx\n")
        return json.dumps(_RESULT)  # an older CLI: one JSON object, no events

    calls: list = []
    log = _run_chain(chain, monkeypatch, repo, behaviour, calls)
    assert _git(repo, "rev-parse", "HEAD") == head
    assert "not attributable" in log and "could not be attributed" in log


# ═════════════════════════════════════════════════════════════════════════
# 7. Through the real hook siblings (fake claude, real chain, real git)
# ═════════════════════════════════════════════════════════════════════════

_FAKE_REVIEWER = r"""#!/usr/bin/env bash
cat > /dev/null
root="$(pwd)"
mkdir -p "$root/knowledge"
printf -- '---\ntitle: Hook node\n---\n\nFrom the hook run.\n' > "$root/knowledge/hook-node.md"
printf 'reviewer edit\n' >> "$root/docs.md"
printf '{"type":"system","subtype":"init"}\n'
printf '{"type":"assistant","message":{"content":[{"type":"tool_use","id":"a","name":"Write","input":{"file_path":"%s/knowledge/hook-node.md"}}]}}\n' "$root"
printf '{"type":"assistant","message":{"content":[{"type":"tool_use","id":"b","name":"Edit","input":{"file_path":"%s/docs.md"}}]}}\n' "$root"
printf '{"type":"result","subtype":"success","num_turns":3,"result":"done"}\n'
"""


def _git_env(repo: Path, env: dict, *args: str) -> str:
    return _REAL_RUN(["git", *args], cwd=repo, env=env, check=True,
                     capture_output=True, text=True).stdout


@pytest.mark.parametrize("sibling", ["sh", "ps1"])
def test_hook_end_to_end_commits_only_the_reviewer_node(tmp_path, sibling):
    import stat
    import time
    from tests.test_v02102_w6n_kg_reviewer_fixes import (
        _hook_cmd, _install_chain, _scratch_repo)
    hook = _hook_cmd(sibling)  # skips when pwsh is absent
    repo, env = _scratch_repo(tmp_path, commits=1)
    fake = tmp_path / "bin" / "claude"
    fake.write_text(_FAKE_REVIEWER, encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    _install_chain(repo, env)
    (repo / ".gitignore").write_text(".claude/\n")
    (repo / "docs.md").write_text("doc\n")
    _git_env(repo, env, "add", ".gitignore", "docs.md")
    _git_env(repo, env, "commit", "-q", "-m", "feat: docs")
    (repo / "first_file.txt").write_text("user staged\n")
    _git_env(repo, env, "add", "first_file.txt")
    reviewed = _git_env(repo, env, "rev-parse", "HEAD").strip()
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "git commit"},
                          "tool_response": "[main abc] committed"})
    proc = _REAL_RUN(hook, cwd=repo, env=env, input=payload, capture_output=True,
                     text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    log = repo / ".claude" / "logs" / "kg-commit-review.log"
    deadline = time.monotonic() + 90
    text = ""
    while time.monotonic() < deadline:
        text = log.read_text(encoding="utf-8") if log.exists() else ""
        if "committed " in text or "commit skipped" in text or "commit failed" in text:
            break
        time.sleep(0.3)
    assert "committed " in text, text
    head = _git_env(repo, env, "rev-parse", "HEAD").strip()
    assert head != reviewed
    shown = _git_env(repo, env, "show", "--name-only", "--format=%s", head).split("\n")
    assert shown[0].startswith("kg-sync: ") and "feat: docs" in shown[0], shown
    assert [f for f in shown[1:] if f.strip()] == ["knowledge/hook-node.md"]
    assert (repo / "docs.md").read_text() == "doc\n"  # the docs edit was reverted
    assert _git_env(repo, env, "diff", "--cached", "--name-only").split() == ["first_file.txt"]
